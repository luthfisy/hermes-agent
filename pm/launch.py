"""CLI entry after the isolated interpreter has been selected."""
from pathlib import Path
import sys

import truststore

# PM's import closure constructs HTTPS clients; install platform trust first.
truststore.inject_into_ssl()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm.cli import main
from pm.runtime import lease_current_runtime

if __name__ == "__main__":
    lease_current_runtime()
    raise SystemExit(main())
