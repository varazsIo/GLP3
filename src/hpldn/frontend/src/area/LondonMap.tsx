import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { DISTRICT_SHAPES, DISTRICT_SHAPE_BY_CODE, MAP_VIEW_BOX, THAMES_PATH } from '../data/londonGeo';
import { formatGBPCompact } from '../lib/format';
import { RAMP, rampIndexFor } from './MapLegend';
import styles from './LondonMap.module.css';

/**
 * Props for `LondonMap`.
 */
export interface LondonMapProps {
  /**
   * Average price per covered area code. A shape without a price (TN, CM) is
   * uncovered: drawn in the idle fill and not selectable.
   */
  districtPrices: Record<string, number>;
  /** The shading breaks, `PRICE_BREAKS`, shared with the legend. */
  priceBreaks: ReadonlyArray<number>;
  /** Code of the selected area, if any. */
  selectedDistrict: string | null;
  /** Called with an area code when a shape is chosen. */
  onSelectDistrict: (code: string) => void;
  /** The key to the shading, set straight under the map, above the licence line. */
  legend?: React.ReactNode;
}

const [BASE_X, BASE_Y, BASE_W, BASE_H] = MAP_VIEW_BOX.split(' ').map(Number);
/** Zoom factors; the last steps separate EC1-EC4 and WC1/WC2 in the centre. */
const ZOOM_STEPS = [1, 1.6, 2.6, 4, 7];
/** The zoom a phone opens at, where the central shapes are big enough to tap. */
const PHONE_ZOOM_STEP = 2;
const PHONE_QUERY = '(max-width: 640px)';
/** A touch within this many CSS pixels of an area's label point picks that area. */
const TAP_RADIUS_PX = 16;
/** Pointer travel, in CSS pixels, after which a press is a drag, not a tap. */
const DRAG_THRESHOLD_PX = 6;
/** How long after a handled touch the browser's emulated click is ignored. */
const EMULATED_CLICK_MS = 600;
/** Space kept between the tooltip and the frame's edge, in CSS pixels. */
const TIP_INSET_PX = 4;

interface Point {
  x: number;
  y: number;
}

/**
 * Returns the centre of the view at a zoom step, clamped so the view stays inside the map.
 */
function clampCentre(step: number, centre: Point): Point {
  const zoom = ZOOM_STEPS[step];
  const halfW = BASE_W / zoom / 2;
  const halfH = BASE_H / zoom / 2;
  return {
    x: Math.min(BASE_X + BASE_W - halfW, Math.max(BASE_X + halfW, centre.x)),
    y: Math.min(BASE_Y + BASE_H - halfH, Math.max(BASE_Y + halfH, centre.y)),
  };
}

/**
 * Returns the viewBox for a zoom step, centred on a point and clamped inside the map.
 */
function viewBoxFor(step: number, centre: Point): string {
  const zoom = ZOOM_STEPS[step];
  const width = BASE_W / zoom;
  const height = BASE_H / zoom;
  const clamped = clampCentre(step, centre);
  return [clamped.x - width / 2, clamped.y - height / 2, width, height].join(' ');
}

/**
 * Returns the zoom step the page opens at: closer in on a phone.
 */
function initialZoomStep(): number {
  if (typeof window === 'undefined' || !window.matchMedia) {
    return 0;
  }
  return window.matchMedia(PHONE_QUERY).matches ? PHONE_ZOOM_STEP : 0;
}

/**
 * Returns the zoom step closest to a zoom factor.
 */
function nearestStep(zoom: number): number {
  let best = 0;
  ZOOM_STEPS.forEach((step, index) => {
    if (Math.abs(Math.log(step / zoom)) < Math.abs(Math.log(ZOOM_STEPS[best] / zoom))) {
      best = index;
    }
  });
  return best;
}

/**
 * A crosshair icon: a ring, a centre dot and four ticks.
 */
function TargetIcon(): React.ReactElement {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="6.5" fill="none" stroke="currentColor" strokeWidth="2" />
      <circle cx="12" cy="12" r="2.5" fill="currentColor" />
      <path d="M12 1.5v4M12 18.5v4M1.5 12h4M18.5 12h4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

/**
 * A minus icon, or a plus icon when `plus` is set.
 */
function PlusMinusIcon({ plus = false }: { plus?: boolean }): React.ReactElement {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d={plus ? 'M5 12h14M12 5v14' : 'M5 12h14'} stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Greater London by postcode area, shaded by average price.
 *
 * Choosing a covered area moves the page to it; five zoom steps keep the
 * selection centred. The view pans by dragging (mouse or finger) and zooms by
 * pinching, double-tapping or the 44px buttons, and phones open zoomed in. A
 * tap picks the area whose label point is within 16px, so the smallest
 * central shapes can still be chosen. Shapes have hover and focus tooltips,
 * kept inside the frame and clear of the controls, and are keyboard operable;
 * neither a touch nor a mouse leaving the map leaves a tooltip behind. Uncovered shapes are neither buttons nor tab stops.
 */
export function LondonMap({
  districtPrices,
  priceBreaks,
  selectedDistrict,
  onSelectDistrict,
  legend,
}: LondonMapProps): React.ReactElement {
  const [zoomStep, setZoomStep] = useState(initialZoomStep);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [hovered, setHovered] = useState<string | null>(null);
  const [tipBox, setTipBox] = useState<{ left: number; top: number } | null>(null);

  const frameRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  const controlsRef = useRef<HTMLDivElement>(null);
  const pointers = useRef(new Map<number, Point>());
  const gesture = useRef<{ dragging: boolean; travel: number; pinchStart: number; pinchStep: number }>({
    dragging: false,
    travel: 0,
    pinchStart: 0,
    pinchStep: 0,
  });
  const lastTouchAt = useRef(0);

  const selected = useMemo(
    () => (selectedDistrict ? DISTRICT_SHAPE_BY_CODE[selectedDistrict] : undefined),
    [selectedDistrict],
  );

  const base: Point = selected
    ? { x: selected.labelX, y: selected.labelY }
    : { x: BASE_X + BASE_W / 2, y: BASE_Y + BASE_H / 2 };
  const viewBox = viewBoxFor(zoomStep, { x: base.x + pan.x, y: base.y + pan.y });
  const [viewX, viewY, viewW, viewH] = viewBox.split(' ').map(Number);

  // A new selection is centred again.
  useEffect(() => setPan({ x: 0, y: 0 }), [selectedDistrict]);

  /** Moves the view so its centre is `centre`, clamped inside the map at `step`. */
  const centreOn = useCallback(
    (step: number, centre: Point) => {
      const clamped = clampCentre(step, centre);
      setPan({ x: clamped.x - base.x, y: clamped.y - base.y });
    },
    [base.x, base.y],
  );

  const changeZoom = (step: number): void => {
    const next = Math.max(0, Math.min(ZOOM_STEPS.length - 1, step));
    setZoomStep(next);
    centreOn(next, { x: viewX + viewW / 2, y: viewY + viewH / 2 });
  };

  /** Converts a client point to map units in the current view. */
  const toMap = (clientX: number, clientY: number): Point | null => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || !rect.width || !rect.height) {
      return null;
    }
    return {
      x: viewX + ((clientX - rect.left) / rect.width) * viewW,
      y: viewY + ((clientY - rect.top) / rect.height) * viewH,
    };
  };

  /** Returns the covered area whose label point is nearest a map point, within `TAP_RADIUS_PX`. */
  const nearestCovered = (point: Point): string | null => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || !rect.width) {
      return null;
    }
    const radius = (TAP_RADIUS_PX / rect.width) * viewW;
    let best: string | null = null;
    let bestDistance = radius;
    DISTRICT_SHAPES.forEach((shape) => {
      if (!districtPrices[shape.code]) {
        return;
      }
      const distance = Math.hypot(shape.labelX - point.x, shape.labelY - point.y);
      if (distance <= bestDistance) {
        best = shape.code;
        bestDistance = distance;
      }
    });
    return best;
  };

  const onPointerDown = (event: React.PointerEvent<SVGSVGElement>): void => {
    if (event.pointerType === 'touch') {
      lastTouchAt.current = Date.now();
      setHovered(null);
    }
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size === 1) {
      gesture.current = { ...gesture.current, dragging: false, travel: 0 };
    } else if (pointers.current.size === 2) {
      const [a, b] = Array.from(pointers.current.values());
      gesture.current = {
        ...gesture.current,
        dragging: true,
        pinchStart: Math.hypot(a.x - b.x, a.y - b.y),
        pinchStep: zoomStep,
      };
    }
  };

  const onPointerMove = (event: React.PointerEvent<SVGSVGElement>): void => {
    const previous = pointers.current.get(event.pointerId);
    if (!previous) {
      return;
    }
    const current = { x: event.clientX, y: event.clientY };
    pointers.current.set(event.pointerId, current);

    if (pointers.current.size >= 2) {
      const [a, b] = Array.from(pointers.current.values());
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (gesture.current.pinchStart > 0) {
        const step = nearestStep(ZOOM_STEPS[gesture.current.pinchStep] * (distance / gesture.current.pinchStart));
        if (step !== zoomStep) {
          changeZoom(step);
        }
      }
      return;
    }

    const dx = current.x - previous.x;
    const dy = current.y - previous.y;
    gesture.current.travel += Math.hypot(dx, dy);
    if (!gesture.current.dragging && gesture.current.travel > DRAG_THRESHOLD_PX) {
      gesture.current.dragging = true;
      svgRef.current?.setPointerCapture?.(event.pointerId);
    }
    const rect = svgRef.current?.getBoundingClientRect();
    if (gesture.current.dragging && zoomStep > 0 && rect && rect.width) {
      const scale = viewW / rect.width;
      centreOn(zoomStep, { x: viewX + viewW / 2 - dx * scale, y: viewY + viewH / 2 - dy * scale });
    }
  };

  const onPointerEnd = (event: React.PointerEvent<SVGSVGElement>): void => {
    const wasTap = pointers.current.size === 1 && !gesture.current.dragging;
    pointers.current.delete(event.pointerId);
    if (pointers.current.size < 2) {
      gesture.current.pinchStart = 0;
    }
    if (event.type !== 'pointerup' || event.pointerType !== 'touch') {
      return;
    }
    lastTouchAt.current = Date.now();
    setHovered(null);
    if (!wasTap) {
      return;
    }
    const point = toMap(event.clientX, event.clientY);
    const near = point ? nearestCovered(point) : null;
    const under = (event.target as Element).closest?.('path[data-code]')?.getAttribute('data-code') ?? null;
    const code = near ?? (under && districtPrices[under] ? under : null);
    if (code) {
      onSelectDistrict(code);
    }
  };

  const onDoubleClick = (event: React.MouseEvent<SVGSVGElement>): void => {
    const point = toMap(event.clientX, event.clientY);
    const next = Math.min(ZOOM_STEPS.length - 1, zoomStep + 1);
    setZoomStep(next);
    if (point) {
      centreOn(next, point);
    }
  };

  /** True while the browser is replaying a touch as mouse events, which the touch handlers already used. */
  const fromTouch = (): boolean => Date.now() - lastTouchAt.current < EMULATED_CLICK_MS;

  const select = (code: string): void => {
    if (fromTouch() || gesture.current.dragging) {
      return;
    }
    onSelectDistrict(code);
  };

  const hover = (code: string): void => {
    if (!fromTouch()) {
      setHovered(code);
    }
  };

  const tip = hovered ? DISTRICT_SHAPE_BY_CODE[hovered] : undefined;
  const tipPrice = tip ? districtPrices[tip.code] : undefined;

  // Place the tooltip over its area, kept inside the frame: flipped below the
  // point near the top edge, slid along near the sides, and moved off the map
  // controls. It is positioned by transform from the frame's top left, so its
  // measured width is its natural width wherever the last tooltip stood.
  useLayoutEffect(() => {
    const frame = frameRef.current;
    const svg = svgRef.current;
    const tipElement = tipRef.current;
    if (!tip || !frame || !svg || !tipElement) {
      setTipBox(null);
      return;
    }
    const frameRect = frame.getBoundingClientRect();
    const svgRect = svg.getBoundingClientRect();
    const anchorX = svgRect.left - frameRect.left + ((tip.labelX - viewX) / viewW) * svgRect.width;
    const anchorY = svgRect.top - frameRect.top + ((tip.labelY - viewY) / viewH) * svgRect.height;
    const width = tipElement.offsetWidth;
    const height = tipElement.offsetHeight;
    const maxLeft = Math.max(TIP_INSET_PX, frameRect.width - width - TIP_INSET_PX);
    const maxTop = Math.max(TIP_INSET_PX, frameRect.height - height - TIP_INSET_PX);
    const above = anchorY - height - 10;
    let left = Math.min(maxLeft, Math.max(TIP_INSET_PX, anchorX - width / 2));
    const top = Math.min(maxTop, above >= TIP_INSET_PX ? above : anchorY + 10);

    const controlsRect = controlsRef.current?.getBoundingClientRect();
    if (controlsRect && controlsRect.width) {
      const controlsLeft = controlsRect.left - frameRect.left;
      const controlsTop = controlsRect.top - frameRect.top;
      const overlaps = left + width > controlsLeft - TIP_INSET_PX && top + height > controlsTop - TIP_INSET_PX;
      if (overlaps) {
        left = Math.max(TIP_INSET_PX, controlsLeft - width - TIP_INSET_PX);
      }
    }
    setTipBox({ left, top });
  }, [tip, viewX, viewY, viewW, viewH]);

  return (
    <div className={styles.wrap}>
      <div className={styles.frame} ref={frameRef} onMouseLeave={() => setHovered(null)}>
        <svg
          className={styles.svg}
          ref={svgRef}
          viewBox={viewBox}
          role="group"
          aria-label="Map of London. Postcode districts shaded by average price."
          style={{ touchAction: zoomStep > 0 ? 'none' : 'pan-y' }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerEnd}
          onPointerCancel={onPointerEnd}
          onPointerLeave={(event) => {
            if (event.pointerType === 'mouse') {
              setHovered(null);
            }
          }}
          onDoubleClick={onDoubleClick}
        >
          <g onMouseLeave={() => setHovered(null)}>
            {DISTRICT_SHAPES.map((shape) => {
              const price = districtPrices[shape.code];
              if (!price) {
                return (
                  <path
                    key={shape.code}
                    d={shape.path}
                    fill="var(--c-map-idle)"
                    className={[styles.district, styles.uncovered].join(' ')}
                    data-code={shape.code}
                    onMouseEnter={() => hover(shape.code)}
                  />
                );
              }
              const isSelected = shape.code === selectedDistrict;
              return (
                <path
                  key={shape.code}
                  d={shape.path}
                  fill={RAMP[rampIndexFor(price, priceBreaks)]}
                  className={styles.district}
                  data-code={shape.code}
                  tabIndex={0}
                  role="button"
                  aria-pressed={isSelected}
                  aria-label={shape.code + ' · ' + shape.label + ', average price ' + formatGBPCompact(price)}
                  onClick={() => select(shape.code)}
                  onMouseEnter={() => hover(shape.code)}
                  onFocus={() => setHovered(shape.code)}
                  onBlur={() => setHovered(null)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      onSelectDistrict(shape.code);
                    }
                  }}
                />
              );
            })}
            {/* The selected outline on top, so its neighbours do not paint over its edge. */}
            {selected && districtPrices[selected.code] ? (
              <path className={styles.selectedOutline} d={selected.path} aria-hidden="true" />
            ) : null}
          </g>
          <path className={styles.river} d={THAMES_PATH} />
        </svg>

        {tip ? (
          <span
            className={styles.tip}
            ref={tipRef}
            style={
              tipBox ? { transform: 'translate(' + tipBox.left + 'px, ' + tipBox.top + 'px)' } : { visibility: 'hidden' }
            }
            aria-hidden="true"
          >
            {tipPrice ? (
              <>
                <span className={styles.tipName}>{tip.code + ' · ' + tip.label}</span>
                <span className={styles.tipValue}>{formatGBPCompact(tipPrice) + ' average'}</span>
              </>
            ) : (
              <span className={styles.tipName}>{tip.code + ' · not covered'}</span>
            )}
          </span>
        ) : null}

        {/* Bottom right, the way a web map puts them: re-centre above, zoom below. */}
        <div className={styles.controls} ref={controlsRef}>
          <button
            type="button"
            className={[styles.control, styles.recentre].join(' ')}
            aria-label="Reset map view"
            title="Reset map view"
            onClick={() => {
              setZoomStep(0);
              setPan({ x: 0, y: 0 });
            }}
            disabled={zoomStep === 0}
          >
            <TargetIcon />
          </button>
          <div className={styles.zoom} role="group" aria-label="Zoom">
            <button
              type="button"
              className={styles.control}
              aria-label="Zoom in"
              title="Zoom in"
              disabled={zoomStep === ZOOM_STEPS.length - 1}
              onClick={() => changeZoom(zoomStep + 1)}
            >
              <PlusMinusIcon plus />
            </button>
            <span className={styles.zoomDivider} aria-hidden="true" />
            <button
              type="button"
              className={styles.control}
              aria-label="Zoom out"
              title="Zoom out"
              disabled={zoomStep === 0}
              onClick={() => changeZoom(zoomStep - 1)}
            >
              <PlusMinusIcon />
            </button>
          </div>
        </div>
      </div>

      {legend}

      <p className={styles.caption}>
        Each area is shaded based on the average price of the property having the maximum value shown in the
        box.
      </p>
    </div>
  );
}

export default LondonMap;
