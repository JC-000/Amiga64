import sys
from pathlib import Path

# Make `import host`/`import tests` work regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
