"""The website / dashboard server: start, stop, status."""

from __future__ import annotations


def cmd_ui(args, console):
    """ui"""
    from arbkit.site.process import status, stop
    from arbkit.site.server import serve

    if args.stop:
        return stop(console)
    if args.status:
        return status(console)
    serve(args.port, args.output_dir, console, public=args.public, host=args.host, live_db=args.live_db)
    return 0
