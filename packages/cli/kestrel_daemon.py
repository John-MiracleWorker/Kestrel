from kestrel_cli import daemon_app as _daemon_app

globals().update({name: value for name, value in vars(_daemon_app).items() if not name.startswith("__")})
