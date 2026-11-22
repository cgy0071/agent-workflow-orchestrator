"""Keep the actual child PID when Windows uses a virtualenv redirector."""
import os
import sys


def process_options(env=None):
    if os.name == "nt" and sys.prefix != sys.base_prefix:
        return {"executable": sys._base_executable,
                "env": dict(os.environ if env is None else env, __PYVENV_LAUNCHER__=sys.executable)}
    return {"env": env}
