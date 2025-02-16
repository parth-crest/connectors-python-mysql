#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
import asyncio
import json
import os
from copy import deepcopy
from datetime import datetime
from unittest import mock
from unittest.mock import ANY, AsyncMock, Mock, call

import pytest
from aioresponses import CallbackResult

from connectors.byoc import (
    CONNECTORS_INDEX,
    STUCK_JOBS_THRESHOLD,
    Connector,
    ConnectorIndex,
    Features,
    Filter,
    Filtering,
    JobStatus,
    Status,
    SyncJob,
    SyncJobIndex,
    e2str,
    iso_utc,
)
from connectors.byoei import ElasticServer
from connectors.config import load_config
from connectors.filtering.validation import ValidationTarget
from connectors.logger import logger
from connectors.source import BaseDataSource
from connectors.tests.commons import AsyncGeneratorFake

CONFIG = os.path.join(os.path.dirname(__file__), "config.yml")

DEFAULT_DOMAIN = "DEFAULT"

DRAFT_ADVANCED_SNIPPET = {"value": {"query": {"options": {}}}}

DRAFT_RULE_ONE_ID = 1
DRAFT_RULE_TWO_ID = 2

ACTIVE_ADVANCED_SNIPPET = {"value": {"find": {"settings": {}}}}

ACTIVE_RULE_ONE_ID = 3
ACTIVE_RULE_TWO_ID = 4

ACTIVE_FILTER_STATE = "active"
DRAFT_FILTER_STATE = "draft"

FILTERING_VALIDATION_VALID = {"state": "valid", "errors": []}

DRAFT_FILTERING_DEFAULT_DOMAIN = {
    "advanced_snippet": DRAFT_ADVANCED_SNIPPET,
    "rules": [{"id": DRAFT_RULE_ONE_ID}, {"id": DRAFT_RULE_TWO_ID}],
    "validation": FILTERING_VALIDATION_VALID,
}

ACTIVE_FILTERING_DEFAULT_DOMAIN = {
    "advanced_snippet": ACTIVE_ADVANCED_SNIPPET,
    "rules": [{"id": ACTIVE_RULE_ONE_ID}, {"id": ACTIVE_RULE_TWO_ID}],
    "validation": FILTERING_VALIDATION_VALID,
}

FILTERING_VALIDATION_DEFAULT_DOMAIN_WITH_ERRORS = {
    "state": "invalid",
    "errors": [
        {"ids": ["1"], "errors": ["some error"]},
        {"ids": ["2"], "errors": ["another error"]},
    ],
}

FILTERING_DEFAULT_DOMAIN_DRAFT_AFTER_UPDATE = {
    "domain": DEFAULT_DOMAIN,
    "draft": DRAFT_FILTERING_DEFAULT_DOMAIN
    | {"validation": FILTERING_VALIDATION_DEFAULT_DOMAIN_WITH_ERRORS},
    "active": ACTIVE_FILTERING_DEFAULT_DOMAIN,
}

FILTERING_DEFAULT_DOMAIN_ACTIVE_AFTER_UPDATE = {
    "domain": DEFAULT_DOMAIN,
    "draft": DRAFT_FILTERING_DEFAULT_DOMAIN,
    "active": ACTIVE_FILTERING_DEFAULT_DOMAIN
    | {"validation": FILTERING_VALIDATION_DEFAULT_DOMAIN_WITH_ERRORS},
}

FILTERING_DEFAULT_DOMAIN = {
    "domain": DEFAULT_DOMAIN,
    "draft": DRAFT_FILTERING_DEFAULT_DOMAIN,
    "active": ACTIVE_FILTERING_DEFAULT_DOMAIN,
}

FILTERING_OTHER_DOMAIN = {
    "domain": "other",
    "draft": {},
    "active": {},
    "validation": {
        "state": "invalid",
        "errors": [{"ids": ["1"], "messages": ["some messages"]}],
    },
}

CONNECTOR_ID = 1

DOC_SOURCE_FILTERING = [FILTERING_DEFAULT_DOMAIN, FILTERING_OTHER_DOMAIN]

DOC_SOURCE = {
    "configuration": {"key": "value"},
    "description": "description",
    "error": "none",
    "features": {},
    "filtering": DOC_SOURCE_FILTERING,
    "index_name": "search-index",
    "name": "MySQL",
    "pipeline": {},
    "scheduling": {},
    "service_type": "SERVICE",
    "status": "connected",
    "language": "en",
    "sync_now": False,
}

EXPECTED_FILTERING_AFTER_UPDATE_DRAFT = {
    "filtering": [FILTERING_DEFAULT_DOMAIN_DRAFT_AFTER_UPDATE, FILTERING_OTHER_DOMAIN]
}

EXPECTED_FILTERING_AFTER_UPDATE_ACTIVE = {
    "filtering": [FILTERING_DEFAULT_DOMAIN_ACTIVE_AFTER_UPDATE, FILTERING_OTHER_DOMAIN]
}

EXPECTED_UPDATED_DOC_SOURCE_DRAFT_FILTERING = (
    DOC_SOURCE | EXPECTED_FILTERING_AFTER_UPDATE_DRAFT
)
EXPECTED_UPDATED_DOC_SOURCE_ACTIVE_FILTERING = (
    DOC_SOURCE | EXPECTED_FILTERING_AFTER_UPDATE_ACTIVE
)

OTHER_DOMAIN_ONE = "other-domain-1"
OTHER_DOMAIN_TWO = "other-domain-2"
NON_EXISTING_DOMAIN = "non-existing-domain"

EMPTY_FILTER = Filter()

FILTERING = [
    {
        "domain": Filtering.DEFAULT_DOMAIN,
        "draft": DRAFT_FILTERING_DEFAULT_DOMAIN,
        "active": ACTIVE_FILTERING_DEFAULT_DOMAIN,
        "validation": FILTERING_VALIDATION_VALID,
    },
    {
        "domain": OTHER_DOMAIN_ONE,
        "draft": {},
        "active": {},
        "validation": FILTERING_VALIDATION_VALID,
    },
    {
        "domain": OTHER_DOMAIN_TWO,
        "draft": {},
        "active": {},
        "validation": FILTERING_VALIDATION_VALID,
    },
]

EMPTY_FILTERING = Filter()

ADVANCED_RULES_EMPTY = {"advanced_snippet": {}}

ADVANCED_RULES = {"db": {"table": "SELECT * FROM db.table"}}

ADVANCED_RULES_NON_EMPTY = {"advanced_snippet": ADVANCED_RULES}

RULES = [
    {
        "id": 1,
    }
]
BASIC_RULES_NON_EMPTY = {"rules": RULES}
ADVANCED_AND_BASIC_RULES_NON_EMPTY = {
    "advanced_snippet": {"db": {"table": "SELECT * FROM db.table"}},
    "rules": RULES,
}


@pytest.fixture(autouse=True)
def patch_validate_filtering_in_byoc():
    with mock.patch(
        "connectors.byoc.validate_filtering", return_value=AsyncMock()
    ) as validate_filtering_mock:
        yield validate_filtering_mock


def test_e2str():
    # The BYOC protocol uses lower case
    assert e2str(Status.NEEDS_CONFIGURATION) == "needs_configuration"


def test_utc():
    # All dates are in ISO 8601 UTC so we can serialize them
    now = datetime.utcnow()
    then = json.loads(json.dumps({"date": iso_utc(when=now)}))["date"]
    assert now.isoformat() == then


@pytest.mark.asyncio
async def test_sync_job(mock_responses):
    config = {"host": "http://nowhere.com:9200", "user": "tarek", "password": "blah"}
    jobs_index = SyncJobIndex(elastic_config=config)
    client = jobs_index.client

    expected_filtering = {
        "advanced_snippet": {
            # "value" should be omitted by extracting content inside "value" and moving it one level up
            "find": {"settings": {}}
        },
        "rules": [{"id": ACTIVE_RULE_ONE_ID}, {"id": ACTIVE_RULE_TWO_ID}],
        "validation": FILTERING_VALIDATION_VALID,
    }

    job = SyncJob(connector_id="connector-id", elastic_index=jobs_index)

    headers = {"X-Elastic-Product": "Elasticsearch"}
    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors/_refresh", headers=headers
    )

    sent_docs = []

    def callback(url, **kwargs):
        sent_docs.append(json.loads(kwargs["data"]))
        return CallbackResult(
            body=json.dumps({"_id": "1"}), status=200, headers=headers
        )

    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors-sync-jobs/_doc",
        callback=callback,
        headers=headers,
    )

    mock_responses.put(
        "http://nowhere.com:9200/.elastic-connectors-sync-jobs/_doc/1",
        callback=callback,
        headers=headers,
    )
    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors-sync-jobs/_update/1",
        callback=callback,
        repeat=True,
    )

    assert job.duration == -1
    await job.start(filtering=ACTIVE_FILTERING_DEFAULT_DOMAIN)
    assert job.status == JobStatus.IN_PROGRESS
    assert job.job_id is not None
    await asyncio.sleep(0.2)
    await job.done(12, 34)
    assert job.status == JobStatus.COMPLETED
    await client.close()
    assert job.duration >= 0.2

    # verify what was sent
    assert len(sent_docs) == 2
    doc, update = sent_docs
    assert doc["status"] == "in_progress"
    assert doc["connector"]["filtering"] == expected_filtering
    assert update["doc"]["status"] == "completed"
    assert update["doc"]["indexed_document_count"] == 12
    assert update["doc"]["deleted_document_count"] == 34


mongo = {
    "api_key_id": "",
    "configuration": {
        "host": {"value": "mongodb://127.0.0.1:27021", "label": "MongoDB Host"},
        "database": {"value": "sample_airbnb", "label": "MongoDB Database"},
        "collection": {
            "value": "listingsAndReviews",
            "label": "MongoDB Collection",
        },
    },
    "index_name": "search-airbnb",
    "service_type": "mongodb",
    "status": "configured",
    "language": "en",
    "last_sync_status": "null",
    "last_sync_error": "",
    "last_synced": "",
    "last_seen": "",
    "created_at": "",
    "updated_at": "",
    "scheduling": {"enabled": True, "interval": "0 * * * *"},
    "sync_now": True,
}


@pytest.mark.asyncio
async def test_heartbeat(mock_responses, patch_logger):
    config = {"host": "http://nowhere.com:9200", "user": "tarek", "password": "blah"}
    headers = {"X-Elastic-Product": "Elasticsearch"}
    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors/_refresh", headers=headers
    )

    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors/_search?expand_wildcards=hidden",
        payload={
            "hits": {"hits": [{"_id": "1", "_source": mongo}], "total": {"value": 1}}
        },
        headers=headers,
    )

    for i in range(10):
        mock_responses.put(
            "http://nowhere.com:9200/.elastic-connectors/_doc/1",
            payload={"_id": "1"},
            headers=headers,
        )

    connectors = ConnectorIndex(config)
    conns = []

    query = connectors.build_docs_query([["mongodb"]])
    async for connector in connectors.get_all_docs(query=query):
        connector.start_heartbeat(0.2)
        connector.start_heartbeat(1.0)  # NO-OP
        conns.append(connector)

    await asyncio.sleep(0.4)
    await conns[0].close()
    await connectors.close()


@pytest.mark.asyncio
async def test_connectors_get_list(mock_responses):
    config = {"host": "http://nowhere.com:9200", "user": "tarek", "password": "blah"}
    headers = {"X-Elastic-Product": "Elasticsearch"}
    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors/_refresh", headers=headers
    )

    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors/_search?expand_wildcards=hidden",
        payload={
            "hits": {"hits": [{"_id": "1", "_source": mongo}], "total": {"value": 1}}
        },
        headers=headers,
    )

    connectors = ConnectorIndex(config)
    conns = []
    query = connectors.build_docs_query([["mongodb"]])
    async for connector in connectors.get_all_docs(query=query):
        conns.append(connector)

    assert len(conns) == 1
    await connectors.close()


class StubIndex:
    def __init__(self):
        self.client = None

    async def save(self, connector):
        pass


doc = {"_id": 1}
max_concurrency = 0


class Data(BaseDataSource):
    def __init__(self, connector):
        super().__init__(connector)
        self.concurrency = 0

    @classmethod
    def get_default_configuration(cls):
        return {}

    async def ping(self):
        pass

    async def changed(self):
        return True

    async def lazy(self, doit=True, timestamp=None):
        if not doit:
            return
        self.concurrency += 1
        global max_concurrency
        max_concurrency = 0

        if self.concurrency > max_concurrency:
            max_concurrency = self.concurrency
            logger.info(f"max_concurrency {max_concurrency}")
        try:
            await asyncio.sleep(0.01)
            return {"extra_data": 100}
        finally:
            self.concurrency -= 1

    async def get_docs(self, *args, **kw):
        for d in [doc] * 100:
            yield {"_id": 1}, self.lazy

    async def close(self):
        pass

    def tweak_bulk_options(self, options):
        options["concurrent_downloads"] = 3


@pytest.mark.parametrize("with_filtering", [True, False])
@pytest.mark.asyncio
async def test_sync_mongo(
    with_filtering, mock_responses, patch_logger, patch_validate_filtering_in_byoc
):
    config = {"host": "http://nowhere.com:9200", "user": "tarek", "password": "blah"}
    headers = {"X-Elastic-Product": "Elasticsearch"}
    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors/_refresh", headers=headers
    )

    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors/_search?expand_wildcards=hidden",
        payload={
            "hits": {"hits": [{"_id": "1", "_source": mongo}], "total": {"value": 1}}
        },
        headers=headers,
    )
    mock_responses.put(
        "http://nowhere.com:9200/.elastic-connectors/_doc/1",
        payload={"_id": "1"},
        headers=headers,
    )
    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors/_update/1",
        headers=headers,
        repeat=True,
    )
    mock_responses.put(
        "http://nowhere.com:9200/.elastic-connectors/_doc/1",
        payload={"_id": "1"},
        headers=headers,
    )
    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors-sync-jobs/_doc",
        payload={"_id": "1"},
        headers=headers,
    )
    mock_responses.post(
        "http://nowhere.com:9200/.elastic-connectors-sync-jobs/_update/1",
        headers=headers,
        repeat=True,
    )
    mock_responses.put(
        "http://nowhere.com:9200/.elastic-connectors-sync-jobs/_doc/1",
        payload={"_id": "1"},
        headers=headers,
    )
    mock_responses.head(
        "http://nowhere.com:9200/search-airbnb?expand_wildcards=open",
        headers=headers,
        repeat=True,
    )
    mock_responses.get(
        "http://nowhere.com:9200/search-airbnb/_mapping?expand_wildcards=open",
        payload={"search-airbnb": {"mappings": {}}},
        headers=headers,
    )
    mock_responses.put(
        "http://nowhere.com:9200/search-airbnb/_mapping?expand_wildcards=open",
        headers=headers,
    )
    mock_responses.get(
        "http://nowhere.com:9200/search-airbnb",
        payload={"hits": {"hits": [{"_id": "1", "_source": mongo}]}},
        headers=headers,
    )
    mock_responses.get(
        "http://nowhere.com:9200/search-airbnb/_search?scroll=5m",
        payload={"hits": {"hits": [{"_id": "1", "_source": mongo}]}},
        headers=headers,
    )
    mock_responses.post(
        "http://nowhere.com:9200/search-airbnb/_search?scroll=5m",
        payload={"_id": "1"},
        headers=headers,
    )
    mock_responses.put(
        "http://nowhere.com:9200/search-airbnb/_search?scroll=5m",
        payload={"_id": "1"},
        headers=headers,
    )
    mock_responses.put(
        "http://nowhere.com:9200/_bulk?pipeline=ent-search-generic-ingestion",
        payload={"items": []},
        headers=headers,
    )

    es = ElasticServer(config)
    connectors = ConnectorIndex(config)
    service_config = {"sources": {"mongodb": "connectors.tests.test_byoc:Data"}}

    try:
        query = connectors.build_docs_query([["mongodb"]])
        async for connector in connectors.get_all_docs(query=query):
            connector.features.sync_rules_enabled = Mock(return_value=with_filtering)

            await connector.prepare(service_config)
            await connector.sync(es, 0)
            await connector.close()
    finally:
        await connectors.close()
        await es.close()

    if with_filtering:
        assert patch_validate_filtering_in_byoc.call_count
    else:
        assert not patch_validate_filtering_in_byoc.call_count

    # verify that the Data source was able to override the option
    patch_logger.assert_not_present("max_concurrency 10")
    patch_logger.assert_present("max_concurrency 3")


@pytest.mark.asyncio
async def test_properties(mock_responses):
    connector_src = {
        "service_type": "test",
        "index_name": "search-some-index",
        "configuration": {},
        "language": "en",
        "scheduling": {},
        "status": "created",
    }

    connector = Connector(StubIndex(), "test", connector_src, {})

    assert connector.status == Status.CREATED
    assert connector.service_type == "test"
    connector.service_type = "test2"
    assert connector.service_type == "test2"
    assert connector._dirty

    await connector.sync_doc()
    assert not connector._dirty

    # setting some config with a value that is None
    connector.configuration = {"cool": {"value": "foo"}, "cool2": {"value": None}}

    assert connector.status == Status.NEEDS_CONFIGURATION

    # setting some config
    connector.configuration = {"cool": {"value": "foo"}, "cool2": {"value": "baz"}}

    assert connector.status == Status.CONFIGURED

    with pytest.raises(TypeError):
        connector.status = 1234


class Banana(BaseDataSource):
    """Banana"""

    @classmethod
    def get_default_configuration(cls):
        return {"one": {"value": None}}


@pytest.mark.asyncio
async def test_prepare(mock_responses):
    class Client:
        pass

    class Index:
        client = Client()

        async def save(self, conn):
            pass

    # generic empty doc created by the user through the Kibana UI
    # when it's created that way, the service type is None,
    # so it's up to the connector to set it back to its value
    doc = {
        "status": "created",
        "service_type": None,
        "index_name": "test",
        "configuration": {},
        "language": "en",
        "scheduling": {"enabled": False},
    }
    connector = Connector(Index(), "1", doc, {})

    config = {
        "connector_id": "1",
        "service_type": "mongodb",
        "sources": {"mongodb": "connectors.tests.test_byoc:Banana"},
    }

    await connector.prepare(config)
    assert connector.source_klass.__doc__ == "Banana"
    assert connector.status == Status.NEEDS_CONFIGURATION


@pytest.mark.parametrize(
    "filtering_json, filter_state, domain, expected_filter",
    [
        (
            FILTERING,
            ACTIVE_FILTER_STATE,
            Filtering.DEFAULT_DOMAIN,
            ACTIVE_FILTERING_DEFAULT_DOMAIN,
        ),
        (
            FILTERING,
            DRAFT_FILTER_STATE,
            Filtering.DEFAULT_DOMAIN,
            DRAFT_FILTERING_DEFAULT_DOMAIN,
        ),
        (FILTERING, ACTIVE_FILTER_STATE, OTHER_DOMAIN_ONE, EMPTY_FILTER),
        (FILTERING, ACTIVE_FILTER_STATE, OTHER_DOMAIN_TWO, EMPTY_FILTER),
        # domains which do not exist should return an empty filter per default
        (FILTERING, ACTIVE_FILTER_STATE, NON_EXISTING_DOMAIN, EMPTY_FILTER),
        # if filtering is not present always return an empty filter
        ([], ACTIVE_FILTER_STATE, Filtering.DEFAULT_DOMAIN, EMPTY_FILTER),
        ([], ACTIVE_FILTER_STATE, NON_EXISTING_DOMAIN, EMPTY_FILTER),
        (None, ACTIVE_FILTER_STATE, Filtering.DEFAULT_DOMAIN, EMPTY_FILTER),
        (None, ACTIVE_FILTER_STATE, NON_EXISTING_DOMAIN, EMPTY_FILTER),
    ],
)
def test_get_filter(filtering_json, filter_state, domain, expected_filter):
    filtering = Filtering(filtering_json)

    assert filtering.get_filter(filter_state, domain) == expected_filter


@pytest.mark.parametrize(
    "domain, expected_filter",
    [
        (DEFAULT_DOMAIN, ACTIVE_FILTERING_DEFAULT_DOMAIN),
        (None, ACTIVE_FILTERING_DEFAULT_DOMAIN),
    ],
)
def test_get_active_filter(domain, expected_filter):
    filtering = Filtering(FILTERING)

    if domain is not None:
        assert filtering.get_active_filter(domain) == expected_filter
    else:
        assert filtering.get_active_filter() == expected_filter


@pytest.mark.parametrize(
    "domain, expected_filter",
    [
        (DEFAULT_DOMAIN, DRAFT_FILTERING_DEFAULT_DOMAIN),
        (None, DRAFT_FILTERING_DEFAULT_DOMAIN),
    ],
)
def test_get_draft_filter(domain, expected_filter):
    filtering = Filtering(FILTERING)

    if domain is not None:
        assert filtering.get_draft_filter(domain) == expected_filter
    else:
        assert filtering.get_draft_filter() == expected_filter


@pytest.mark.parametrize(
    "filtering, expected_transformed_filtering",
    [
        (
            {"advanced_snippet": {"value": {"query": {}}}, "rules": []},
            {"advanced_snippet": {"query": {}}, "rules": []},
        ),
        (
            {"advanced_snippet": {"value": {}}, "rules": []},
            {"advanced_snippet": {}, "rules": []},
        ),
        ({"advanced_snippet": {}, "rules": []}, {"advanced_snippet": {}, "rules": []}),
        ({}, {"advanced_snippet": {}, "rules": []}),
        (None, {"advanced_snippet": {}, "rules": []}),
    ],
)
def test_transform_filtering(filtering, expected_transformed_filtering):
    assert SyncJob.transform_filtering(filtering) == expected_transformed_filtering


@pytest.mark.parametrize(
    "validation_result, validation_target, expected_doc_source_update",
    [
        (
            FILTERING_VALIDATION_DEFAULT_DOMAIN_WITH_ERRORS,
            ValidationTarget.DRAFT,
            EXPECTED_UPDATED_DOC_SOURCE_DRAFT_FILTERING,
        ),
        (
            FILTERING_VALIDATION_DEFAULT_DOMAIN_WITH_ERRORS,
            ValidationTarget.ACTIVE,
            EXPECTED_UPDATED_DOC_SOURCE_ACTIVE_FILTERING,
        ),
    ],
)
@pytest.mark.asyncio
async def test_update_filtering_validation(
    validation_result, validation_target, expected_doc_source_update
):
    config = {"host": "https://nowhere.com:9200", "user": "tarek", "password": "blah"}

    connector = Mock()
    connector.doc_source = DOC_SOURCE
    connector.id = CONNECTOR_ID

    validation_result_mock = Mock()
    validation_result_mock.to_dict = Mock(return_value=validation_result)

    client = Mock()
    client.update = AsyncMock(return_value=1)

    index = ConnectorIndex(config)
    index.client = client

    await index.update_filtering_validation(
        connector, validation_result_mock, validation_target
    )

    assert client.update.call_args_list == [
        call(
            index=CONNECTORS_INDEX,
            id=CONNECTOR_ID,
            doc=expected_doc_source_update,
            retry_on_conflict=ANY,
        )
    ]


@pytest.mark.parametrize(
    "features_json, feature_enabled",
    [
        (
            {
                "sync_rules": {
                    "basic": {"enabled": True},
                    "advanced": {"enabled": True},
                }
            },
            {
                Features.BASIC_RULES_NEW: True,
                Features.ADVANCED_RULES_NEW: True,
                Features.BASIC_RULES_OLD: False,
                Features.ADVANCED_RULES_OLD: False,
            },
        ),
        (
            {
                "sync_rules": {
                    "basic": {"enabled": True},
                    "advanced": {"enabled": False},
                }
            },
            {
                Features.BASIC_RULES_NEW: True,
                Features.ADVANCED_RULES_NEW: False,
                Features.BASIC_RULES_OLD: False,
                Features.ADVANCED_RULES_OLD: False,
            },
        ),
        (
            {
                "sync_rules": {
                    "basic": {"enabled": False},
                    "advanced": {"enabled": False},
                }
            },
            {
                Features.BASIC_RULES_NEW: False,
                Features.ADVANCED_RULES_NEW: False,
                Features.BASIC_RULES_OLD: False,
                Features.ADVANCED_RULES_OLD: False,
            },
        ),
        (
            {"filtering_advanced_config": True, "filtering_rules": True},
            {
                Features.BASIC_RULES_NEW: False,
                Features.ADVANCED_RULES_NEW: False,
                Features.BASIC_RULES_OLD: True,
                Features.ADVANCED_RULES_OLD: True,
            },
        ),
        (
            {"filtering_advanced_config": False, "filtering_rules": False},
            {
                Features.BASIC_RULES_NEW: False,
                Features.ADVANCED_RULES_NEW: False,
                Features.BASIC_RULES_OLD: False,
                Features.ADVANCED_RULES_OLD: False,
            },
        ),
        (
            {"filtering_advanced_config": True, "filtering_rules": False},
            {
                Features.BASIC_RULES_NEW: False,
                Features.ADVANCED_RULES_NEW: False,
                Features.BASIC_RULES_OLD: False,
                Features.ADVANCED_RULES_OLD: True,
            },
        ),
        (
            {
                "sync_rules": {
                    "basic": {"enabled": True},
                    "advanced": {"enabled": True},
                },
                "filtering_advanced_config": True,
                "filtering_rules": True,
            },
            {
                Features.BASIC_RULES_NEW: True,
                Features.ADVANCED_RULES_NEW: True,
                Features.BASIC_RULES_OLD: True,
                Features.ADVANCED_RULES_OLD: True,
            },
        ),
        (
            None,
            {
                Features.BASIC_RULES_NEW: False,
                Features.ADVANCED_RULES_NEW: False,
                Features.BASIC_RULES_OLD: False,
                Features.ADVANCED_RULES_OLD: False,
            },
        ),
        (
            {},
            {
                Features.BASIC_RULES_NEW: False,
                Features.ADVANCED_RULES_NEW: False,
                Features.BASIC_RULES_OLD: False,
                Features.ADVANCED_RULES_OLD: False,
            },
        ),
    ],
)
def test_feature_enabled(features_json, feature_enabled):
    features = Features(features_json)

    assert all(
        features.feature_enabled(feature)
        if enabled
        else not features.feature_enabled(feature)
        for feature, enabled in feature_enabled.items()
    )


@pytest.mark.parametrize(
    "features_json, sync_rules_enabled",
    [
        (
            {
                "sync_rules": {
                    "basic": {"enabled": True},
                    "advanced": {"enabled": False},
                },
                "filtering_advanced_config": False,
                "filtering_rules": False,
            },
            True,
        ),
        (
            {
                "sync_rules": {
                    "basic": {"enabled": False},
                    "advanced": {"enabled": True},
                },
                "filtering_advanced_config": False,
                "filtering_rules": False,
            },
            True,
        ),
        (
            {
                "sync_rules": {
                    "basic": {"enabled": False},
                    "advanced": {"enabled": False},
                },
                "filtering_advanced_config": True,
                "filtering_rules": False,
            },
            True,
        ),
        (
            {
                "sync_rules": {
                    "basic": {"enabled": False},
                    "advanced": {"enabled": False},
                },
                "filtering_advanced_config": False,
                "filtering_rules": True,
            },
            True,
        ),
        (
            {
                "sync_rules": {
                    "basic": {"enabled": False},
                    "advanced": {"enabled": False},
                },
                "filtering_advanced_config": False,
                "filtering_rules": False,
            },
            False,
        ),
        ({"other_feature": True}, False),
        (None, False),
        ({}, False),
    ],
)
def test_sync_rules_enabled(features_json, sync_rules_enabled):
    features = Features(features_json)

    assert features.sync_rules_enabled() == sync_rules_enabled


@pytest.mark.parametrize(
    "nested_dict, keys, default, expected",
    [
        # extract True
        ({"a": {"b": {"c": True}}}, ["a", "b", "c"], False, True),
        (
            {"a": {"b": {"c": True}}},
            # "d" doesn't exist -> fall back to False
            ["a", "b", "c", "d"],
            False,
            False,
        ),
        (
            {"a": {"b": {"c": True}}},
            # "wrong_key" doesn't exist -> fall back to False
            ["wrong_key", "b", "c"],
            False,
            False,
        ),
        # fallback to True
        (None, ["a", "b", "c"], True, True),
    ],
)
def test_nested_get(nested_dict, keys, default, expected):
    assert expected == Features(nested_dict)._nested_feature_enabled(keys, default)


def test_pending_job_query_with_connectors_ids(mock_responses, set_env):
    config = load_config(CONFIG)

    connectors_ids = [1, 2]
    sync_job_index = SyncJobIndex(elastic_config=config["elasticsearch"])
    pending_jobs_query = sync_job_index.pending_job_query(connectors_ids=connectors_ids)

    # validate the query
    assert "bool" in pending_jobs_query
    assert pending_jobs_query["bool"] == {
        "must": [
            {"terms": {"status": ["pending"]}},
            {"terms": {"connector.id": connectors_ids}},
        ]
    }


def test_orphaned_jobs_query(mock_responses, set_env):
    config = load_config(CONFIG)

    connectors_ids = [1, 2]
    sync_job_index = SyncJobIndex(elastic_config=config["elasticsearch"])
    orphaned_jobs_query = sync_job_index.orphaned_jobs_query(
        connectors_ids=connectors_ids
    )

    assert orphaned_jobs_query == {
        "bool": {"must_not": {"terms": {"connector.id": connectors_ids}}}
    }


def test_stuck_jobs_query(mock_responses, set_env):
    config = load_config(CONFIG)

    connectors_ids = [1, 2]
    sync_job_index = SyncJobIndex(elastic_config=config["elasticsearch"])
    stuck_jobs_query = sync_job_index.stuck_jobs_query(connectors_ids=connectors_ids)

    assert "bool" in stuck_jobs_query
    assert len(stuck_jobs_query["bool"]["filter"]) == 3
    assert {"terms": {"connector.id": connectors_ids}} in stuck_jobs_query["bool"][
        "filter"
    ]
    assert {
        "terms": {"status": [e2str(JobStatus.IN_PROGRESS), e2str(JobStatus.CANCELING)]}
    } in stuck_jobs_query["bool"]["filter"]

    assert {
        "range": {"last_seen": {"lte": f"now-{STUCK_JOBS_THRESHOLD}s"}}
    } in stuck_jobs_query["bool"]["filter"]


@pytest.mark.parametrize(
    "filtering, should_advanced_rules_be_present",
    [
        (ADVANCED_RULES_NON_EMPTY, True),
        (ADVANCED_AND_BASIC_RULES_NON_EMPTY, True),
        (ADVANCED_RULES_EMPTY, False),
        (BASIC_RULES_NON_EMPTY, False),
        (EMPTY_FILTERING, False),
        (None, False),
    ],
)
def test_advanced_rules_present(filtering, should_advanced_rules_be_present):
    assert Filter(filtering).has_advanced_rules() == should_advanced_rules_be_present


@pytest.mark.parametrize(
    "filtering, expected_advanced_rules",
    (
        [
            (ADVANCED_RULES_NON_EMPTY, ADVANCED_RULES),
            (ADVANCED_AND_BASIC_RULES_NON_EMPTY, ADVANCED_RULES),
            (ADVANCED_RULES_EMPTY, {}),
            (BASIC_RULES_NON_EMPTY, {}),
            (EMPTY_FILTERING, {}),
            (None, {}),
        ]
    ),
)
def test_extract_advanced_rules(filtering, expected_advanced_rules):
    assert Filter(filtering).get_advanced_rules() == expected_advanced_rules


@pytest.mark.parametrize(
    "filtering, expected_filtering_calls",
    [
        (None, [Filter()]),
        (
            Filter({"advanced_snippet": {}, "rules": []}),
            [Filter({"advanced_snippet": {}, "rules": []})],
        ),
    ],
)
@pytest.mark.asyncio
async def test_prepare_docs(filtering, expected_filtering_calls):
    doc_source_copy = deepcopy(DOC_SOURCE)
    connector = Connector(StubIndex(), "1", doc_source_copy, {})

    docs_generator_fake = AsyncGeneratorFake([(doc_source_copy, None)])
    connector.data_provider = AsyncMock()
    connector.data_provider.get_docs = docs_generator_fake

    async for yielded_doc in connector.prepare_docs(
        connector.data_provider, filtering=filtering
    ):
        assert yielded_doc is not None

    assert docs_generator_fake.call_kwargs == [
        ("filtering", expected_filtering)
        for expected_filtering in expected_filtering_calls
    ]
    assert all(
        type(filter_) == Filter for _, filter_ in docs_generator_fake.call_kwargs
    )
