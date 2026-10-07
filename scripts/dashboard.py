"""Arc dashboard launcher. Takes the same flags as modern.server.

Local:  python3 -m scripts.dashboard
AWS:    python3 -m scripts.dashboard --remote https://<api-id>.execute-api.us-east-1.amazonaws.com --profile natoros

Serves the Arc build (web/dist/index.html, from `npm ci && npm run build` in web/) when it exists,
otherwise the classic modern/dashboard.html. It lives outside modern/ so the Lambda package, and
the deployed code hash recorded in docs/evidence, stay unchanged.
"""
import sys
from pathlib import Path

from modern import server

ARC_BUILD = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"


def select_dashboard(build=ARC_BUILD):
    return build if build.is_file() else server.DASHBOARD


def main(argv=None):
    page = select_dashboard()
    if page == ARC_BUILD:
        print(f"UI: Arc build ({page})")
    else:
        print("UI: classic dashboard; run `npm ci && npm run build` in web/ for the Arc UI", file=sys.stderr)
    server.DASHBOARD = page
    server.main(argv)


if __name__ == "__main__":
    main()
