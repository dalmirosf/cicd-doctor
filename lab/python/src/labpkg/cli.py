"""Case py-modnf: requests is imported but not declared in pyproject.toml."""

import requests


def main() -> None:
    print(requests.__version__)


if __name__ == "__main__":
    main()
