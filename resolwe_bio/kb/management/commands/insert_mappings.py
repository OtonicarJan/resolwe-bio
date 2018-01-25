""".. Ignore pydocstyle D400.

==============================
Insert Knowledge Base Mappings
==============================

"""
from __future__ import absolute_import, division, print_function, unicode_literals
import csv
import json
import logging

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import connection

from resolwe.elastic.builder import index_builder
from resolwe.utils import BraceMessage as __

from resolwe_bio.kb.models import Mapping
from .utils import decompress


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Command(BaseCommand):
    """Insert knowledge base mappings."""

    help = "Insert knowledge base mappings"

    def add_arguments(self, parser):
        """Command arguments."""
        parser.add_argument('file_name', type=str, help="Tab-separated file with mappings (supports tab, gz or zip)")

    def handle(self, *args, **options):
        """Command handle."""
        count_total, count_inserted = 0, 0
        to_index = []

        relation_type_choices = list(zip(*Mapping.RELATION_TYPE_CHOICES))[0]

        for tab_file_name, tab_file in decompress(options['file_name']):
            logger.info(__("Importing mappings from \"{}\"...", tab_file_name))

            mappings = set()
            for row in csv.DictReader(tab_file, delimiter=str('\t')):
                if row['relation_type'] not in relation_type_choices:
                    raise ValidationError(
                        "Unknown relation type: {}".format(row['relation_type'])
                    )

                # NOTE: For performance reasons this is a tuple instead of a dict.
                #       Tuple can be hashed, so it can be used in `ìn` operation,
                #       and is serialized to a JSON list.
                #       Make sure that any changes also reflect in the SQL query
                #       below.
                mapping = (
                    row['relation_type'],
                    row['source_db'],
                    row['source_id'],
                    row['source_species'],
                    row['target_db'],
                    row['target_id'],
                    row['target_species'],
                )

                if mapping in mappings:
                    raise ValidationError(
                        "Duplicated mapping (relation type: '{}', source db: '{}', source id: "
                        "'{}', source species: {}, target db: '{}', target id: '{}', "
                        "target species: {}) found in '{}'".format(
                            row['relation_type'], row['source_db'], row['source_id'],
                            row['source_species'], row['target_db'], row['target_id'],
                            row['target_species'], tab_file_name
                        )
                    )

                mappings.add(mapping)

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH tmp AS(
                        INSERT INTO {table_name} (
                            relation_type, source_db, source_id, source_species,
                            target_db, target_id, target_species
                        )
                        SELECT
                            value->>0, value->>1, value->>2, value->>3,
                            value->>4, value->>5, value->>6
                        FROM json_array_elements(%s)
                        LEFT JOIN {table_name}
                            ON value->>0 = {table_name}.relation_type
                            AND value->>1 = {table_name}.source_db
                            AND value->>2 = {table_name}.source_id
                            AND value->>3 = {table_name}.source_species
                            AND value->>4 = {table_name}.target_db
                            AND value->>5 = {table_name}.target_id
                            AND value->>6 = {table_name}.target_species
                        WHERE {table_name}.relation_type IS NULL
                        RETURNING id
                    )
                    SELECT
                        COALESCE(array_agg(id), ARRAY[]::INTEGER[]) AS ids,
                        COUNT(*) AS count_inserted
                    FROM tmp;
                    """.format(
                        table_name=Mapping._meta.db_table,  # pylint: disable=no-member,protected-access
                    ),
                    params=[json.dumps(list(mappings))]
                )
                result = cursor.fetchone()

            to_index.extend(result[0])

            count_total += len(mappings)
            count_inserted += result[1]

        index_builder.build(queryset=Mapping.objects.filter(id__in=to_index))

        logger.info(  # pylint: disable=logging-not-lazy
            "Total mappings: %d. Inserted %d, unchanged %d." %
            (count_total, count_inserted, count_total - count_inserted)
        )
