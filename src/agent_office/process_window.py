"""Creation flags for non-interactive child processes with redirected or inherited streams."""
import os
import subprocess


BACKGROUND_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
