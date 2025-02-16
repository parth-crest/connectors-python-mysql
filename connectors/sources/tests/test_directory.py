#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
import pytest

from connectors.sources.directory import HERE, DirectoryDataSource
from connectors.sources.tests.support import assert_basics, create_source


@pytest.mark.asyncio
async def test_basics():
    await assert_basics(DirectoryDataSource, "directory", HERE)


@pytest.mark.asyncio
async def test_get_docs(patch_logger, catch_stdout):
    source = create_source(DirectoryDataSource)
    num = 0
    async for (doc, dl) in source.get_docs():
        num += 1
        if "__init__.py" in doc["path"]:
            continue
        data = await dl(doit=True, timestamp="xx")
        assert len(data["_attachment"]) > 0

    assert num > 3
