# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import exc, sql
from sqlalchemy.engine.base import Connection
from sqlalchemy.engine.default import DefaultDialect, DefaultExecutionContext
from sqlalchemy.engine.url import URL

from trino import dbapi as trino_dbapi
from trino.auth import BasicAuthentication
from trino.dbapi import Cursor

from . import compiler, datatype, error


class TrinoDialect(DefaultDialect):
    name = 'trino'
    driver = 'rest'

    statement_compiler = compiler.TrinoSQLCompiler
    ddl_compiler = compiler.TrinoDDLCompiler
    type_compiler = compiler.TrinoTypeCompiler
    preparer = compiler.TrinoIdentifierPreparer

    # Data Type
    supports_native_enum = False
    supports_native_boolean = True
    supports_native_decimal = True

    # Column options
    supports_sequences = False
    supports_comments = True
    inline_comments = True
    supports_default_values = False

    # DDL
    supports_alter = True

    # DML
    supports_empty_insert = False
    supports_multivalues_insert = True
    postfetch_lastrowid = False

    @classmethod
    def dbapi(cls):
        """
        ref: https://www.python.org/dev/peps/pep-0249/#module-interface
        """
        return trino_dbapi

    def create_connect_args(self, url: URL) -> Tuple[List[Any], Dict[str, Any]]:
        args, kwargs = super(TrinoDialect, self).create_connect_args(url)  # type: List[Any], Dict[str, Any]

        db_parts = kwargs.pop('database', 'hive').split('/')
        if len(db_parts) == 1:
            kwargs['catalog'] = db_parts[0]
        elif len(db_parts) == 2:
            kwargs['catalog'] = db_parts[0]
            kwargs['schema'] = db_parts[1]
        else:
            raise ValueError(f'Unexpected database format {url.database}')

        username = kwargs.pop('username', 'anonymous')
        kwargs['user'] = username

        password = kwargs.pop('password', None)
        if password:
            kwargs['http_scheme'] = 'https'
            kwargs['auth'] = BasicAuthentication(username, password)

        return args, kwargs

    def get_columns(self, connection: Connection,
                    table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        if not self.has_table(connection, table_name, schema):
            raise exc.NoSuchTableError(f'schema={schema}, table={table_name}')
        return self._get_columns(connection, table_name, schema, **kw)

    def _get_columns(self, connection: Connection,
                     table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        schema = schema or self._get_default_schema_name(connection)
        query = dedent('''
            SELECT
                "column_name",
                "column_default",
                "is_nullable",
                "data_type"
            FROM "information_schema"."columns"
            WHERE "table_schema" = :schema AND "table_name" = :table
            ORDER BY "ordinal_position" ASC
        ''').strip()
        res = connection.execute(sql.text(query), schema=schema, table=table_name)
        columns = []
        for record in res:
            column = dict(
                name=record.column_name,
                type=datatype.parse_sqltype(record.data_type),
                nullable=(record.is_nullable or '').upper() == 'YES',
                default=record.column_default,
            )
            columns.append(column)
        return columns

    def get_pk_constraint(self, connection: Connection,
                          table_name: str, schema: str = None, **kw) -> Dict[str, Any]:
        """Trino has no support for primary keys. Returns a dummy"""
        return dict(name=None, constrained_columns=[])

    def get_primary_keys(self, connection: Connection,
                         table_name: str, schema: str = None, **kw) -> List[str]:
        pk = self.get_pk_constraint(connection, table_name, schema)
        return pk.get('constrained_columns')

    def get_foreign_keys(self, connection: Connection,
                         table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        """Trino has no support for foreign keys. Returns an empty list."""
        return []

    def get_schema_names(self, connection: Connection, **kw) -> List[str]:
        query = 'SHOW SCHEMAS'
        res = connection.execute(sql.text(query))
        return [row.Schema for row in res]

    def get_table_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        query = 'SHOW TABLES'
        if schema:
            query = f'{query} FROM {self.identifier_preparer.quote_identifier(schema)}'
        res = connection.execute(sql.text(query))
        return [row.Table for row in res]

    def get_temp_table_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        """Trino has no support for temporary tables. Returns an empty list."""
        return []

    def get_view_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        schema = schema or self._get_default_schema_name(connection)
        if schema is None:
            raise exc.NoSuchTableError('schema is required')
        query = dedent('''
            SELECT "table_name"
            FROM "information_schema"."views"
            WHERE "table_schema" = :schema
        ''').strip()
        res = connection.execute(sql.text(query), schema=schema)
        return [row.table_name for row in res]

    def get_temp_view_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        """Trino has no support for temporary views. Returns an empty list."""
        return []

    def get_view_definition(self, connection: Connection, view_name: str, schema: str = None, **kw) -> str:
        full_view = self._get_full_table(view_name, schema)
        query = f'SHOW CREATE VIEW {full_view}'
        try:
            res = connection.execute(sql.text(query))
            return res.scalar()
        except error.TrinoQueryError as e:
            if e.error_name in (
                    error.TABLE_NOT_FOUND,
                    error.SCHEMA_NOT_FOUND,
                    error.CATALOG_NOT_FOUND,
            ):
                raise exc.NoSuchTableError(full_view) from e
            raise

    def get_indexes(self, connection: Connection,
                    table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        if not self.has_table(connection, table_name, schema):
            raise exc.NoSuchTableError(f'schema={schema}, table={table_name}')

        partitioned_columns = self._get_columns(connection, f'{table_name}$partitions', schema, **kw)
        partition_index = dict(
            name='partition',
            column_names=[col['name'] for col in partitioned_columns],
            unique=False
        )
        return [partition_index, ]

    def get_unique_constraints(self, connection: Connection,
                               table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        """Trino has no support for unique constraints. Returns an empty list."""
        return []

    def get_check_constraints(self, connection: Connection,
                              table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        """Trino has no support for check constraints. Returns an empty list."""
        return []

    def get_table_comment(self, connection: Connection,
                          table_name: str, schema: str = None, **kw) -> Dict[str, Any]:
        properties_table = self._get_full_table(f"{table_name}$properties", schema)
        query = f'SELECT "comment" FROM {properties_table}'
        try:
            res = connection.execute(sql.text(query))
            return dict(text=res.scalar())
        except error.TrinoQueryError as e:
            if e.error_name in (
                    error.NOT_FOUND,
                    error.COLUMN_NOT_FOUND,
                    error.TABLE_NOT_FOUND,
            ):
                return dict(text=None)
            raise

    def has_schema(self, connection: Connection, schema: str) -> bool:
        query = f"SHOW SCHEMAS LIKE '{schema}'"
        try:
            res = connection.execute(sql.text(query))
            return res.first() is not None
        except error.TrinoQueryError as e:
            if e.error_name in (
                    error.TABLE_NOT_FOUND,
                    error.SCHEMA_NOT_FOUND,
                    error.CATALOG_NOT_FOUND,
            ):
                return False
            raise

    def has_table(self, connection: Connection,
                  table_name: str, schema: str = None) -> bool:
        query = 'SHOW TABLES'
        if schema:
            query = f'{query} FROM {self.identifier_preparer.quote_identifier(schema)}'
        query = f"{query} LIKE '{table_name}'"
        try:
            res = connection.execute(sql.text(query))
            return res.first() is not None
        except error.TrinoQueryError as e:
            if e.error_name in (
                    error.TABLE_NOT_FOUND,
                    error.SCHEMA_NOT_FOUND,
                    error.CATALOG_NOT_FOUND,
                    error.MISSING_SCHEMA_NAME,
            ):
                return False
            raise

    def has_sequence(self, connection: Connection,
                     sequence_name: str, schema: str = None) -> bool:
        """Trino has no support for sequence. Returns False indicate that given sequence does not exists."""
        return False

    def _get_server_version_info(self, connection: Connection) -> Tuple[int, ...]:
        query = 'SELECT version()'
        res = connection.execute(sql.text(query))
        version = res.scalar()
        return tuple([version])

    def _get_default_schema_name(self, connection: Connection) -> Optional[str]:
        dbapi_connection: trino_dbapi.Connection = connection.connection
        return dbapi_connection.schema

    def do_execute(self, cursor: Cursor, statement: str, parameters: Tuple[Any, ...],
                   context: DefaultExecutionContext = None):
        cursor.execute(statement, parameters)
        if context and context.should_autocommit:
            # SQL statement only submitted to Trino server when cursor.fetch*() is called.
            # For DDL (CREATE/ALTER/DROP) and DML (INSERT/UPDATE/DELETE) statement, call cursor.description
            # to force submit statement immediately.
            cursor.description # noqa

    def do_rollback(self, dbapi_connection):
        if dbapi_connection.transaction is not None:
            dbapi_connection.rollback()

    def do_begin_twophase(self, connection: Connection, xid):
        pass

    def do_prepare_twophase(self, connection: Connection, xid):
        pass

    def do_rollback_twophase(self, connection: Connection, xid,
                             is_prepared: bool = True, recover: bool = False) -> None:
        pass

    def do_commit_twophase(self, connection: Connection, xid,
                           is_prepared: bool = True, recover: bool = False) -> None:
        pass

    def do_recover_twophase(self, connection: Connection) -> None:
        pass

    def set_isolation_level(self, dbapi_conn: trino_dbapi.Connection, level) -> None:
        dbapi_conn._isolation_level = getattr(trino_dbapi.IsolationLevel, level)

    def get_isolation_level(self, dbapi_conn: trino_dbapi.Connection) -> str:
        level_names = ['AUTOCOMMIT',
                       'READ_UNCOMMITTED',
                       'READ_COMMITTED',
                       'REPEATABLE_READ',
                       'SERIALIZABLE']
        return level_names[dbapi_conn.isolation_level]

    def _get_full_table(self, table_name: str, schema: str = None, quote: bool = True) -> str:
        table_part = self.identifier_preparer.quote_identifier(table_name) if quote else table_name
        if schema:
            schema_part = self.identifier_preparer.quote_identifier(schema) if quote else schema
            return f'{schema_part}.{table_part}'

        return table_part
