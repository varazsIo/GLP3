"""``python -m hpldn.backend.cloud.oci``: see `hpldn.backend.cloud.oci.provision`."""

import sys

from hpldn.backend.cloud.oci.provision import main

if __name__ == "__main__":
    sys.exit(main())
