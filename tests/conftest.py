import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from rednotebook.domain.models import SourceGrant
from rednotebook.fixtures import make_fixture
from rednotebook.storage import Database


@pytest.fixture
def clock():
    return [datetime(2026, 9, 16, 12, tzinfo=timezone.utc)]


@pytest.fixture
def fixture_data(clock):
    return make_fixture(clock[0])


@pytest.fixture
def grant(fixture_data):
    return SourceGrant.model_validate(fixture_data[0])


@pytest.fixture
def db(tmp_path, clock):
    with Database(tmp_path / "test.sqlite", clock=lambda: clock[0]) as database:
        yield database


@pytest.fixture
def write_rows(tmp_path):
    def write(rows, name="rows.json"):
        file = tmp_path / name
        file.write_text(json.dumps(deepcopy(rows), ensure_ascii=False))
        return file

    return write
