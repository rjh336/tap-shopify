"""
Test tap discovery
"""
import re

from tap_tester import menagerie

from base import BaseTapTest


class DiscoveryTest(BaseTapTest):
    """ Test the tap discovery """

    @staticmethod
    def name():
        return "tap_tester_shopify_discovery_test"

    def test_run(self):
        """
        Verify that discover creates the appropriate catalog, schema, metadata, etc.

        • Verify number of actual streams discovered match expected
        • Verify the stream names discovered were what we expect
        • Verify stream names follow naming convention
          streams should only have lowercase alphas and underscores
        • verify there is only 1 top level breadcrumb
        • verify replication key(s)
        • verify primary key(s)
        • verify that if there is a replication key we are doing INCREMENTAL otherwise FULL
        • verify the actual replication matches our expected replication method
        • verify that primary, replication and foreign keys
          are given the inclusion of automatic (metadata and annotated schema).
        • verify that all other fields have inclusion of available (metadata and schema)
        """
        conn_id = self.create_connection()
        expected_streams = self.expected_streams() | {"abandoned_checkouts"}

        # Verify number of actual streams discovered match expected
        found_catalogs = menagerie.get_catalogs(conn_id)
        self.assertGreater(len(found_catalogs), 0,
                           msg="unable to locate schemas for connection {}".format(conn_id))
        self.assertEqual(len(found_catalogs),
                         len(expected_streams),
                         msg="Expected {} streams, actual was {} for connection {},"
                             " actual {}".format(
                                 len(expected_streams),
                                 len(found_catalogs),
                                 found_catalogs,
                                 conn_id))

        # Verify the stream names discovered were what we expect
        found_catalog_names = {c['tap_stream_id'] for c in found_catalogs}
        self.assertEqual(set(expected_streams),
                         set(found_catalog_names),
                         msg="Expected streams don't match actual streams")

        # Verify stream names follow naming convention
        # streams should only have lowercase alphas and underscores
        self.assertTrue(all([re.fullmatch(r"[a-z_]+", name) for name in found_catalog_names]),
                        msg="One or more streams don't follow standard naming")

        for stream in expected_streams:
            with self.subTest(stream=stream):
                catalog = next(iter([catalog for catalog in found_catalogs
                                     if catalog["stream_name"] == stream]))
                assert catalog  # based on previous tests this should always be found

                schema_and_metadata = menagerie.get_annotated_schema(conn_id, catalog['stream_id'])
                metadata = schema_and_metadata["metadata"]
                schema = schema_and_metadata["annotated-schema"]

                # verify the stream level properties are as expected
                # verify there is only 1 top level breadcrumb
                stream_properties = [item for item in metadata if item.get("breadcrumb") == []]
                self.assertTrue(len(stream_properties) == 1,
                                msg="There is more than one top level breadcrumb")

                # collect fields
                actual_fields = []
                for md_entry in metadata:
                    if md_entry['breadcrumb'] != []:
                        actual_fields.append(md_entry['breadcrumb'][1])
                # Verify there are no duplicate/conflicting metadata entries.
                self.assertEqual(len(actual_fields), len(set(actual_fields)), msg="There are duplicate entries in the fields of '{}' stream".format(stream))

                # verify replication key(s)
                self.assertEqual(
                    set(stream_properties[0].get(
                        "metadata", {self.REPLICATION_KEYS: []}).get(self.REPLICATION_KEYS, [])),
                    self.expected_replication_keys()[stream],
                    msg="expected replication key {} but actual is {}".format(
                        self.expected_replication_keys()[stream],
                        set(stream_properties[0].get(
                            "metadata", {self.REPLICATION_KEYS: None}).get(
                            self.REPLICATION_KEYS, []))))

                # verify primary key(s)
                self.assertEqual(
                    set(stream_properties[0].get(
                        "metadata", {self.PRIMARY_KEYS: []}).get(self.PRIMARY_KEYS, [])),
                    self.expected_primary_keys()[stream],
                    msg="expected primary key {} but actual is {}".format(
                        self.expected_primary_keys()[stream],
                        set(stream_properties[0].get(
                            "metadata", {self.PRIMARY_KEYS: None}).get(self.PRIMARY_KEYS, []))))

                # verify that if there is a replication key we are doing INCREMENTAL otherwise FULL
                actual_replication_method = stream_properties[0].get(
                    "metadata", {self.REPLICATION_METHOD: None}).get(self.REPLICATION_METHOD)
                if stream_properties[0].get(
                        "metadata", {self.REPLICATION_KEYS: []}).get(self.REPLICATION_KEYS, []):

                    self.assertTrue(actual_replication_method == self.INCREMENTAL,
                                    msg="Expected INCREMENTAL replication "
                                        "since there is a replication key")
                else:
                    self.assertTrue(actual_replication_method == self.FULL,
                                    msg="Expected FULL replication "
                                        "since there is no replication key")

                # verify the actual replication matches our expected replication method
                self.assertEqual(
                    self.expected_replication_method().get(stream, None),
                    actual_replication_method,
                    msg="The actual replication method {} doesn't match the expected {}".format(
                        actual_replication_method,
                        self.expected_replication_method().get(stream, None)))

                expected_primary_keys = self.expected_primary_keys()[stream]
                expected_replication_keys = self.expected_replication_keys()[stream]
                expected_automatic_fields = expected_primary_keys | expected_replication_keys

                # verify that primary, replication and foreign keys
                # are given the inclusion of automatic in annotated schema.
                actual_automatic_fields = {key for key, value in schema["properties"].items()
                                           if value.get("inclusion") == "automatic"}
                self.assertEqual(expected_automatic_fields,
                                 actual_automatic_fields,
                                 msg="expected {} automatic fields but got {}".format(
                                     expected_automatic_fields,
                                     actual_automatic_fields))

                # verify that all other fields have inclusion of available or unsupported
                self.assertTrue(
                    all(
                        (
                            value.get("inclusion") == "available"
                            or value.get("inclusion") == "unsupported"
                        )
                        for key, value in schema["properties"].items()
                        if key not in actual_automatic_fields
                    ),
                    msg="Not all non key properties are set to available in annotated schema"
                )

                # verify that primary, replication and foreign keys
                # are given the inclusion of automatic in metadata.
                actual_automatic_fields = \
                    {item.get("breadcrumb", ["properties", None])[1]
                     for item in metadata
                     if item.get("metadata").get("inclusion") == "automatic"}
                self.assertEqual(expected_automatic_fields,
                                 actual_automatic_fields,
                                 msg="expected {} automatic fields but got {}".format(
                                     expected_automatic_fields,
                                     actual_automatic_fields))

                # verify that all other fields have inclusion of available or unsupported
                self.assertTrue(
                    all({(item.get("metadata").get("inclusion") == "available" or item.get("metadata").get("inclusion") == "unsupported")
                         for item in metadata
                         if item.get("breadcrumb", []) != []
                         and item.get("breadcrumb", ["properties", None])[1]
                         not in actual_automatic_fields}),
                    msg="Not all non key properties are set to available in metadata")
