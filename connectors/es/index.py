#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
from elasticsearch import ApiError

from connectors.es import ESClient
from connectors.logger import logger

DEFAULT_PAGE_SIZE = 100


class ESIndex(ESClient):
    """
    Encapsulates the work with Elasticsearch index.

    All classes that are extended by ESIndex should implement _create_object
    method to represent documents

    Args:
        index_name (str): index_name: Name of an Elasticsearch index
        elastic_config (dict): Elasticsearch configuration and credentials
    """

    def __init__(self, index_name, elastic_config):
        # initialize elasticsearch client
        super().__init__(elastic_config)
        self.index_name = index_name
        self.elastic_config = elastic_config

    def _create_object(self, doc):
        """
        The method must be implemented in all successor classes

        Args:
            doc (dict): Represents an Elasticsearch document
        Raises:
            NotImplementedError: if not implemented in a successor class
        """
        raise NotImplementedError

    async def get_all_docs(self, query=None, page_size=DEFAULT_PAGE_SIZE):
        """
        Lookup for elasticsearch documents using {query}

        Args:
            query (dict): Represents an Elasticsearch query
            page_size (int): Number of documents per query
        Returns:
            Iterator
        """
        await self.client.indices.refresh(index=self.index_name)

        if query is None:
            query = {"match_all": {}}

        count = 0
        offset = 0

        while True:
            try:
                resp = await self.client.search(
                    index=self.index_name,
                    query=query,
                    from_=offset,
                    size=page_size,
                    expand_wildcards="hidden",
                )
            except ApiError as e:
                logger.critical(f"The server returned {e.status_code}")
                logger.critical(e.body, exc_info=True)
                return

            hits = resp["hits"]["hits"]
            total = resp["hits"]["total"]["value"]
            count += len(hits)
            for hit in hits:
                yield self._create_object(hit)
            if count >= total:
                break
            offset += len(hits)
