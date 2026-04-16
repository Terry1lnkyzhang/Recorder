from src.common.runtime_paths import get_recordings_dir
from src.viewer.window import launch_viewer


if __name__ == "__main__":
    launch_viewer(get_recordings_dir())