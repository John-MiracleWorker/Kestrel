from kestrel_cli import cli_core as _cli_core
from kestrel_cli import cli_output as _cli_output
from kestrel_cli import cli_memory as _cli_memory
from kestrel_cli import cli_main as _cli_main

globals().update({name: value for name, value in vars(_cli_core).items() if not name.startswith("__")})
globals().update({name: value for name, value in vars(_cli_output).items() if not name.startswith("__")})
globals().update({name: value for name, value in vars(_cli_memory).items() if not name.startswith("__")})
globals().update({name: value for name, value in vars(_cli_main).items() if not name.startswith("__")})
