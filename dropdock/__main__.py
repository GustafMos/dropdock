"""`dropdock` opens the launcher window; `dropdock serve ...` runs the server in the terminal."""
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "serve":
        from .server import main as serve
        return serve(argv[1:])
    if argv and argv[0] in ("-V", "--version"):
        from . import __version__
        print("dropdock", __version__)
        return 0
    if argv and argv[0] in ("-h", "--help"):
        print("usage: dropdock            open the launcher window\n"
              "       dropdock serve ...  run the server in this terminal (dropdock serve --help)")
        return 0
    from .launcher import main as gui
    gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
