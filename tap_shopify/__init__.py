#!/usr/bin/env python3
import os
import datetime
import json
import time
import math
import copy

import pyactiveresource
import shopify
import singer
from singer import utils
from singer import metadata
from singer import Transformer
from tap_shopify.context import Context
from tap_shopify.exceptions import ShopifyError
from tap_shopify.streams.base import shopify_error_handling, get_request_timeout, ShopifyAPIError

REQUIRED_CONFIG_KEYS = ["shop", "api_key"]
LOGGER = singer.get_logger()
SDC_KEYS = {'id': 'integer', 'name': 'string', 'myshopify_domain': 'string'}
UNSUPPORTED_FIELDS = {"author"}

@shopify_error_handling
def initialize_shopify_client():
    api_key = Context.config['api_key']
    shop = Context.config['shop']
    version = '2025-01'
    session = shopify.Session(shop, version, api_key)
    shopify.ShopifyResource.activate_session(session)

    # set request timeout
    shopify.Shop.set_timeout(get_request_timeout())

    # Shop.current() makes a call for shop details with provided shop and api_key
    return shopify.Shop.current().attributes

# Add helper
def fetch_app_scopes():
    query = """
    query {
      currentAppInstallation {
        accessScopes {
          handle
        }
      }
    }
    """
    data = json.loads(shopify.GraphQL().execute(query))
    return {s["handle"] for s in data["data"]["currentAppInstallation"]["accessScopes"]}

def has_read_users_access():
    # If the app does not have the 'read_users' scope, return False
    if 'read_users' not in fetch_app_scopes():
        LOGGER.warning(
            "Skipping '%s' field: 'read_users' scope is not granted for public apps.",
            ", ".join(UNSUPPORTED_FIELDS)
        )
        return False
    return True

def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

# Load schemas from schemas folder
def load_schemas():
    schemas = {}

    # This schema represents many of the currency values as JSON schema
    # 'number's, which may result in lost precision.
    for filename in sorted(os.listdir(get_abs_path('schemas'))):
        path = get_abs_path('schemas') + '/' + filename
        schema_name = filename.replace('.json', '')
        with open(path, encoding='UTF-8') as file:
            schemas[schema_name] = json.load(file)

    return schemas


def get_discovery_metadata(stream, schema):
    mdata = metadata.new()
    mdata = metadata.write(mdata, (), 'table-key-properties', stream.key_properties)
    mdata = metadata.write(mdata, (), 'forced-replication-method', stream.replication_method)

    if stream.replication_key:
        mdata = metadata.write(mdata, (), 'valid-replication-keys', [stream.replication_key])

    for field_name in schema['properties'].keys():
        if field_name in stream.key_properties or field_name == stream.replication_key:
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'automatic')
        elif field_name in UNSUPPORTED_FIELDS and not has_read_users_access():
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'unsupported')
        else:
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'available')

    return metadata.to_list(mdata)

def add_synthetic_key_to_schema(schema):
    for k in SDC_KEYS:
        schema['properties']['_sdc_shop_' + k] = {'type': ["null", SDC_KEYS[k]]}
    return schema

def discover():
    initialize_shopify_client() # Checking token in discover mode

    raw_schemas = load_schemas()
    streams = []

    for schema_name, schema in raw_schemas.items():
        if schema_name not in Context.stream_objects:
            continue

        stream = Context.stream_objects[schema_name]()
        catalog_schema = add_synthetic_key_to_schema(schema)

        # create and add catalog entry
        catalog_entry = {
            'stream': schema_name,
            'tap_stream_id': schema_name,
            'schema': catalog_schema,
            'metadata': get_discovery_metadata(stream, schema),
            'key_properties': stream.key_properties,
            'replication_key': stream.replication_key,
            'replication_method': stream.replication_method
        }
        streams.append(catalog_entry)

    return {'streams': streams}

def shuffle_streams(stream_name):
    '''
    Takes the name of the first stream to sync and reshuffles the order
    of the list to put it at the top
    '''
    matching_index = 0
    for i, catalog_entry in enumerate(Context.catalog["streams"]):
        if catalog_entry["tap_stream_id"] == stream_name:
            matching_index = i
    top_half = Context.catalog["streams"][matching_index:]
    bottom_half = Context.catalog["streams"][:matching_index]
    Context.catalog["streams"] = top_half + bottom_half

# pylint: disable=too-many-locals
def sync():
    shop_attributes = initialize_shopify_client()
    sdc_fields = {"_sdc_shop_" + x: shop_attributes[x] for x in SDC_KEYS}
    require_reauth = False

    # If there is a currently syncing stream bookmark, shuffle the
    # stream order so it gets sync'd first
    currently_sync_stream_name = Context.state.get('bookmarks', {}).get('currently_sync_stream')
    if currently_sync_stream_name:
        shuffle_streams(currently_sync_stream_name)

    # Emit all schemas first so we have them for child streams
    for stream in Context.catalog["streams"]:
        if Context.is_selected(stream["tap_stream_id"]):
            singer.write_schema(stream["tap_stream_id"],
                                stream["schema"],
                                stream["key_properties"],
                                bookmark_properties=stream["replication_key"])
            Context.counts[stream["tap_stream_id"]] = 0

    # Loop over streams in catalog
    for catalog_entry in Context.catalog['streams']:
        stream_id = catalog_entry['tap_stream_id']
        stream = Context.stream_objects[stream_id]()

        if not Context.is_selected(stream_id):
            LOGGER.info('Skipping stream: %s', stream_id)
            continue

        LOGGER.info('Syncing stream: %s', stream_id)

        if not Context.state.get('bookmarks'):
            Context.state['bookmarks'] = {}
        Context.state['bookmarks']['currently_sync_stream'] = stream_id
        singer.write_state(Context.state)

        try:
            # some fields have epoch-time as date, hence transform into UTC date
            with Transformer(singer.UNIX_SECONDS_INTEGER_DATETIME_PARSING) as transformer:
                for rec in stream.sync():
                    extraction_time = singer.utils.now()
                    record_schema = catalog_entry['schema']
                    record_metadata = metadata.to_map(catalog_entry['metadata'])
                    rec = transformer.transform({**rec, **sdc_fields},
                                                record_schema,
                                                record_metadata)
                    singer.write_record(stream_id,
                                        rec,
                                        time_extracted=extraction_time)
                    Context.counts[stream_id] += 1
        except ShopifyAPIError as e:
            if stream_id == 'fulfillment_orders' and 'Access denied' in str(e.__cause__):
                require_reauth = True
                continue
            raise e

        Context.state['bookmarks'].pop('currently_sync_stream')
        singer.write_state(Context.state)

    LOGGER.info('----------------------')
    for stream_id, stream_count in Context.counts.items():
        LOGGER.info('%s: %d', stream_id, stream_count)
    LOGGER.info('----------------------')

    if require_reauth:
        raise ShopifyAPIError("Required scopes are missing for the `fulfillment_orders` stream. " \
            "Please re-authorize the connection to sync this stream.")

@utils.handle_top_exception(LOGGER)
def main():
    try:
        # Parse command line arguments
        args = utils.parse_args(REQUIRED_CONFIG_KEYS)

        Context.config = args.config
        Context.state = args.state

        # If discover flag was passed, run discovery mode and dump output to stdout
        if args.discover:
            catalog = discover()
            print(json.dumps(catalog, indent=2))
        # Otherwise run in sync mode
        else:
            Context.tap_start = utils.now()
            if args.catalog:
                Context.catalog = args.catalog.to_dict()
            else:
                Context.catalog = discover()

            sync()
    except pyactiveresource.connection.ResourceNotFound as exc:
        raise ShopifyError(exc, 'Ensure shop is entered correctly') from exc
    except pyactiveresource.connection.UnauthorizedAccess as exc:
        raise ShopifyError(exc, 'Invalid access token - Re-authorize the connection') \
            from exc
    except pyactiveresource.connection.ConnectionError as exc:
        msg = ''
        try:
            body_json = exc.response.body.decode()
            body = json.loads(body_json)
            msg = body.get('errors')
        finally:
            raise ShopifyError(exc, msg) from exc
    except ShopifyError as error:
        raise error
    except Exception as exc:
        raise ShopifyError(exc) from exc

if __name__ == "__main__":
    main()
