import sys
from pathlib import Path

# the tools are scripts, not a package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
