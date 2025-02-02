# Copyright © 2022 Province of British Columbia
#
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
"""Common setup and fixtures for the pytest suite used by this service."""
import os
from contextlib import contextmanager, suppress

import pytest
from flask_migrate import Migrate, upgrade
from ldclient.integrations.test_data import TestData
from sqlalchemy import event, text
from sqlalchemy.schema import DropConstraint, MetaData

from search_api import create_app
from search_api import jwt as _jwt
from search_api.models import db as _db


@contextmanager
def not_raises(exception):
    """Corollary to the pytest raises builtin.

    Assures that an exception is NOT thrown.
    """
    try:
        yield
    except exception:
        raise pytest.fail(f'DID RAISE {exception}')


@pytest.fixture(scope='session')
def ld():
    """LaunchDarkly TestData source."""
    td = TestData.data_source()
    yield td


@pytest.fixture(scope='session')
def app(ld):
    """Return a session-wide application configured in TEST mode."""
    _app = create_app('testing', **{'ld_test_data': ld})

    return _app


@pytest.fixture
def set_env(app):
    """Factory to set environment and Flask config variables."""
    def _set_env(name, value):
        os.environ[name] = value
        app.config[name] = value

    return _set_env


@pytest.fixture(scope='function')
def app_ctx(event_loop):
    """Return a session-wide application configured in TEST mode."""
    _app = create_app('testing')
    with _app.app_context():
        yield _app


@pytest.fixture
def config(app):
    """Return the application config."""
    return app.config


@pytest.fixture(scope='function')
def app_request():
    """Return a session-wide application configured in TEST mode."""
    _app = create_app('testing')

    return _app


@pytest.fixture(scope='session')
def client(app):  # pylint: disable=redefined-outer-name
    """Return a session-wide Flask test client."""
    return app.test_client()


@pytest.fixture(scope='session')
def jwt():
    """Return a session-wide jwt manager."""
    return _jwt


@pytest.fixture(scope='session')
def client_ctx(app):  # pylint: disable=redefined-outer-name
    """Return session-wide Flask test client."""
    with app.test_client() as _client:
        yield _client


@pytest.fixture(scope='session')
def db(app):  # pylint: disable=redefined-outer-name, invalid-name
    """Return a session-wide initialised database.

    Drops all existing tables - Meta follows Postgres FKs
    """
    with app.app_context():
        # Clear out any existing tables
        metadata = MetaData(_db.engine)
        metadata.reflect()
        for table in metadata.tables.values():
            for fk in table.foreign_keys:  # pylint: disable=invalid-name
                with suppress(Exception):
                    _db.engine.execute(DropConstraint(fk.constraint))
        with suppress(Exception):
            metadata.drop_all()
        with suppress(Exception):
            _db.drop_all()

        sequence_sql = """SELECT sequence_name FROM information_schema.sequences
                          WHERE sequence_schema='public'
                       """

        sess = _db.session()
        for seq in [name for (name,) in sess.execute(text(sequence_sql))]:
            with suppress(Exception):
                sess.execute(text('DROP SEQUENCE public.%s ;' % seq))
                print('DROP SEQUENCE public.%s ' % seq)
        sess.commit()

        # ############################################
        # There are 2 approaches, an empty database, or the same one that the app will use
        #     create the tables
        #     _db.create_all()
        # or
        # Use Alembic to load all of the DB revisions including supporting lookup data
        # This is the path we'll use in search_api (same as legal_api)!!

        # even though this isn't referenced directly, it sets up the internal configs that upgrade needs
        Migrate(app, _db)
        upgrade()

        yield _db


@pytest.fixture(scope='function')
def session(db):  # pylint: disable=redefined-outer-name, invalid-name
    """Return a function-scoped session."""
    conn = db.engine.connect()
    txn = conn.begin()

    options = dict(bind=conn, binds={})
    sess = db.create_scoped_session(options=options)

    # establish  a SAVEPOINT just before beginning the test
    # (http://docs.sqlalchemy.org/en/latest/orm/session_transaction.html#using-savepoint)
    sess.begin_nested()

    @event.listens_for(sess(), 'after_transaction_end')
    def restart_savepoint(sess2, trans):  # pylint: disable=unused-variable
        # Detecting whether this is indeed the nested transaction of the test
        if trans.nested and not trans._parent.nested:  # pylint: disable=protected-access
            # Handle where test DOESN'T session.commit(),
            sess2.expire_all()
            sess.begin_nested()

    db.session = sess

    sql = text('select 1')
    sess.execute(sql)

    yield sess

    with suppress(Exception):
        # Cleanup
        sess.remove()
        # This instruction rollsback any commit that were executed in the tests.
        txn.rollback()

        # Fix need here for ResourceClosedError('This Connection is closed') running
        # the test suite. The problem does not occur running a small number of tests,
        # such as in an individual file.
        #
        conn.close()
