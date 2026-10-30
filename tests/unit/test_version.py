import re

import sluice


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+.*|0\+unknown", sluice.__version__)
