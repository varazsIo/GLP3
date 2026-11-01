"""
Model prices for a specific home on a specific day.

``POST /price`` runs the saved CatBoost model (`hpp_cb`) through
`hpldn.backend.scripts.hpp_inference`, which rebuilds the
``production.london_houses`` row a sale of that home on that day would have
and scores it with the model's own saved preprocessing. Nothing is trained per
request: the model is loaded once per worker, and so is the sales history
every price is computed from.

The model is a signed bundle directory (`hpldn.backend.models.artifact`),
resolved by `resolve_model_path`, in order:

1. ``HPLDN_PRICE_MODEL_FP`` (set by the serving host's systemd unit);
2. ``models.catboost_bundle`` in ``configuration/staging.yaml``, relative paths
   taken from the repository root;
3. ``tmp/models/model`` in the repository.

Only that bundle is served: there is no fallback to another model in the
directory, and no Random Forest (a pickle). At warm-up the bundle's signature,
file hashes and JSON schemas are checked and the loaded model must match the
manifest's summary and predict the probe rows exactly; any failure leaves the
routes answering 503. ``HPLDN_ALLOW_UNSIGNED_MODEL=1`` accepts an unsigned
bundle for local development and is refused when ``HPLDN_ENV=production``.
`get_pricer` is cached per worker, so restart the API after changing the model.
"""

import logging
import os
import re
import threading
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from hpldn.backend.api.config import get_settings
from hpldn.backend.api.market.districts import property_type_key, round_to_half_up
from hpldn.backend.api.schemas import AddressOption, LastSale, PriceResult
from hpldn.backend.migrations.address_parse_sql import unit_key
from hpldn.backend.migrations.address_search import _PRICEABLE_PREDICATE, residential_postcodes_cte
from hpldn.backend.models import artifact

MODEL_PATH_ENV = "HPLDN_PRICE_MODEL_FP"
MODEL_CONFIG_KEY = "catboost_bundle"
DEFAULT_MODEL_RELATIVE = Path("tmp") / "models" / "model"

CATBOOST = "catboost"

# The most homes one postcode lists; a large block of flats can hold hundreds.
MAX_ADDRESSES = 500

UNAVAILABLE = "Pricing a specific home is not available right now. Try again later."

log = logging.getLogger("uvicorn.error")


def configured_model_path() -> Path:
    """
    Return the model bundle configuration points at, whether or not it exists.

    ``HPLDN_PRICE_MODEL_FP`` wins, then ``models.catboost_bundle`` in
    ``staging.yaml``, then the repository default. A host without
    ``staging.yaml`` (or with a key naming an unset variable) uses the default.
    """
    from hpldn.backend.common.config import get_cfg, get_repo_root

    configured = os.environ.get(MODEL_PATH_ENV)
    if configured:
        return Path(configured)
    value = None
    try:
        value = (get_cfg().get("models") or {}).get(MODEL_CONFIG_KEY)
    except (FileNotFoundError, KeyError) as exc:
        log.debug("Model path: staging.yaml not usable (%s); using %s", exc, DEFAULT_MODEL_RELATIVE)
    path = Path(value) if value else DEFAULT_MODEL_RELATIVE
    return path if path.is_absolute() else get_repo_root() / path


def resolve_model_path() -> Path | None:
    """
    Return the configured model bundle, or None when there is none there.

    Only a directory holding a ``manifest.json`` counts; it is verified when
    `ModelPricer` loads it, not here.
    """
    configured = configured_model_path()
    return configured if artifact.is_bundle(configured) else None


class ModelPricer:
    """
    The loaded model and its evidence, shared by every request in a worker.

    Loading is lazy and happens once, under a lock: the CatBoost file is a
    few hundred megabytes and the sales history a few hundred thousand rows.
    `warm_in_background` starts it at startup so the first landlord does not
    wait for it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.kind = CATBOOST
        self._service = None
        self._error: Exception | None = None
        self._lock = threading.Lock()

    def service(self):
        """
        Return the loaded `hpp_inference.PricingService`.

        Raises
        ------
        RuntimeError
            When the model or the evidence cannot be loaded; the failure is
            remembered so every request does not retry a missing file.
        """
        if self._service is not None:
            return self._service
        with self._lock:
            if self._service is None and self._error is None:
                try:
                    from hpldn.backend.scripts.hpp_inference import PricingService, SavedModel

                    settings = get_settings()
                    engine = create_engine(
                        settings.database_url,
                        connect_args={"prepare_threshold": None},
                        pool_pre_ping=True,
                    )
                    # Verified (signature, hashes, schemas, summary, probe) before use.
                    model = SavedModel(self.path, kind=CATBOOST)
                    service = PricingService(engine, model, log)
                    service.warm()
                    self._service = service
                    log.info(
                        "Model pricing ready: %s (%s, signed by %s, trained on sales %s), evidence to %s",
                        self.path,
                        self.kind,
                        model.verified.key_id or "nobody: UNSIGNED",
                        " to ".join(service.model.train_period or ["?", "?"]),
                        service.store.latest_sale.date(),
                    )
                except Exception as exc:  # noqa: BLE001 -- reported on every request below
                    self._error = exc
                    log.error("Model pricing unavailable (%s): %s", self.path, exc)
        if self._service is None:
            raise RuntimeError(f"model pricing unavailable: {self._error}")
        return self._service

    def loaded_service(self):
        """Return the pricing service if it has finished loading, else None; never blocks or loads."""
        return self._service

    def warm_in_background(self) -> None:
        """Load the model and evidence on a daemon thread."""
        threading.Thread(target=self._warm, name="hpldn-price-model", daemon=True).start()

    def _warm(self) -> None:
        """Load, swallowing the error `service` has already logged and remembered."""
        try:
            self.service()
        except RuntimeError:
            pass


@lru_cache(maxsize=1)
def get_pricer() -> ModelPricer | None:
    """
    Return this worker's `ModelPricer`, or None when no model bundle is configured (FastAPI dependency).

    Cached: restart the worker after putting a model in place.
    """
    path = resolve_model_path()
    if path is not None:
        return ModelPricer(path)
    log.warning(
        "Model pricing disabled: no model bundle at %s. Set %s or models.%s in configuration/staging.yaml "
        "to a bundle directory, or train one with "
        "python -m hpldn.backend.scripts.hpp_cb --model-out tmp/models/model",
        configured_model_path(),
        MODEL_PATH_ENV,
        MODEL_CONFIG_KEY,
    )
    return None


def loaded_local_index():
    """
    Return the served model's local price index once it has loaded, else None.

    Never loads the model: the market cache calls this to move nearby sales
    along the same index the model price is moved along, and falls back to
    the London index until the model is ready.
    """
    pricer = get_pricer()
    service = pricer.loaded_service() if pricer is not None else None
    return None if service is None else service.local_index


def require_service(pricer: ModelPricer | None):
    """
    Return the pricing service, or raise the 503 a landlord sees.

    Raises
    ------
    HTTPException
        503 when there is no model or it failed to load.
    """
    if pricer is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    try:
        return pricer.service()
    except RuntimeError:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None


# The same priceability test `production.address_search` is built with
# (migrations/address_search.py), narrowed to one postcode, so /addresses and
# /addresses/search list the same homes.
ADDRESSES_SQL = text(
    f"""
    WITH {residential_postcodes_cte("AND a.postcode = :postcode").strip()}
    SELECT a.postnumber, a.poststreet, a.posttown, a.postcode, a.unit,
           {_PRICEABLE_PREDICATE.strip()} AS priceable
    FROM production.address_table a
    WHERE a.postcode = :postcode
    """
)


def _normal(value: str | None) -> str:
    """Return an address part compared the way `hpp_inference.match_address` compares it."""
    return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", (value or "").upper()).split())


def _natural_key(option: AddressOption) -> tuple:
    """Sort addresses by street, then house number as a number, then the rest, then the flat."""
    numbers = [int(n) for n in re.findall(r"\d+", option.postnumber)]
    units = [int(n) for n in re.findall(r"\d+", option.unit or "")]
    return (
        _normal(option.poststreet), numbers[-1] if numbers else 10**9, _normal(option.postnumber),
        units[-1] if units else -1, _normal(option.unit),
    )


def collapse_addresses(rows: list[dict]) -> list[AddressOption]:
    """
    Return one option per home the model can price.

    Homes are keyed on (number, street, unit key), as ``production.address_search``
    folds them since 0031: "Flat 6" and "Apartment 6" of one building are one
    home, flats 6 and 7 two. The register often holds a home twice, with and
    without its street; the blank-street row is folded into the named one. A
    home is listed when any of its rows passes `ADDRESSES_SQL`'s priceability
    test: a usable certificate, a sale of its own, or a number in a
    residential postcode.
    """
    homes: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (_normal(row["postnumber"]), _normal(row["poststreet"]), unit_key(row.get("unit")) or "")
        homes.setdefault(key, {**row, "priceable": False})
        homes[key]["priceable"] = homes[key]["priceable"] or bool(row["priceable"])
    named = {(number, unit) for number, street, unit in homes if street}
    for (number, street, unit), home in list(homes.items()):
        if street or (number, unit) not in named:
            continue
        streets = [key for key in homes if key[0] == number and key[2] == unit and key[1]]
        if len(streets) == 1:
            homes[streets[0]]["priceable"] = homes[streets[0]]["priceable"] or home["priceable"]
            del homes[(number, street, unit)]
    options = [
        AddressOption(
            postnumber=str(home["postnumber"]).strip(),
            poststreet=(home["poststreet"] or "").strip() or None,
            posttown=str(home["posttown"] or "").strip(),
            postcode=home["postcode"],
            unit=(home.get("unit") or "").strip() or None,
        )
        for home in homes.values()
        if home["priceable"] and str(home["postnumber"] or "").strip()
    ]
    return sorted(options, key=_natural_key)[:MAX_ADDRESSES]


async def fetch_addresses(session: AsyncSession, postcode: str) -> list[AddressOption]:
    """Return the homes in `postcode` the model can price, one option each."""
    result = await session.execute(ADDRESSES_SQL, {"postcode": postcode})
    return collapse_addresses([dict(row) for row in result.mappings()])


def to_result(prediction) -> PriceResult:
    """
    Convert an `hpp_inference.PricePrediction` into the wire format.

    The price is rounded to the pound, because it moves along its index every
    day and a £1,000 rounding hid that; the range stays rounded to the £1,000.
    """
    last_sale = None
    if prediction.last_sale_date is not None and prediction.last_sale_price is not None:
        last_sale = LastSale(price=int(prediction.last_sale_price), sold_on=prediction.last_sale_date)
    estimated = getattr(prediction, "epc_source", "certificate") == "estimated"
    return PriceResult(
        address=AddressOption(
            postnumber=prediction.address.postnumber,
            poststreet=prediction.address.poststreet,
            posttown=prediction.address.posttown,
            postcode=prediction.address.postcode,
            unit=getattr(prediction.address, "unit", None),
        ),
        sale_date=prediction.sale_date,
        value=round_to_half_up(prediction.value, 1),
        low=None if prediction.low is None else round_to_half_up(prediction.low, 1000),
        high=None if prediction.high is None else round_to_half_up(prediction.high, 1000),
        property_type=property_type_key(prediction.propertytype, prediction.is_maisonette),
        floor_area_sqm=None if prediction.floor_area_sqm is None else round_to_half_up(prediction.floor_area_sqm, 1),
        # An estimated rating is a neighbourhood's mode, not this home's; it is not shown.
        energy_rating=None if estimated else prediction.energy_rating,
        estimated_energy_rating=prediction.energy_rating if estimated else None,
        habitable_rooms=None if prediction.habitable_rooms is None else float(prediction.habitable_rooms),
        last_sale=last_sale,
        evidence_to=prediction.evidence_to,
        within_ten_percent=prediction.within_10_percent,
        projection_uplift=getattr(prediction, "projection_uplift", None),
        projected_from=getattr(prediction, "projected_from", None),
        index_area=getattr(prediction, "index_area", None),
        epc_source="estimated" if estimated else "certificate",
        estimated_from=getattr(prediction, "estimated_from", None) if estimated else None,
        anchored_on_last_sale=bool(getattr(prediction, "anchored_on_last_sale", False)) and last_sale is not None,
    )
