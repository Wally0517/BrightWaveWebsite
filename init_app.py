import os

from app import app, initialize_app_state


def env_flag(name, default="False"):
    return os.environ.get(name, default).strip().lower() == "true"


if __name__ == "__main__":
    with app.app_context():
        initialize_app_state(
            include_sample_data=env_flag("INIT_SAMPLE_DATA", "True"),
            bootstrap_admin=env_flag("BOOTSTRAP_ADMIN", "False"),
        )
