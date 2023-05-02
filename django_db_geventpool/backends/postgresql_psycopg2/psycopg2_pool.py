# coding=utf-8

# this file is a modified version of the psycopg2 used at gevent examples
# to be compatible with django, also checks if
# DB connection is closed and reopen it:
# https://github.com/surfly/gevent/blob/master/examples/psycopg2_pool.py
import logging
import sys
import weakref

from gevent import queue

logger = logging.getLogger('django.geventpool')


try:
    from psycopg2 import connect, DatabaseError
    import psycopg2.extras
except ImportError as e:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Error loading psycopg2 module: %s" % e)


class DatabaseConnectionPool(object):
    def __init__(self, maxsize=100, reuse=100):
        if not isinstance(maxsize, int):
            raise TypeError('Expected integer, got %r' % (maxsize,))
        if not isinstance(reuse, int):
            raise TypeError('Expected integer, got %r' % (reuse,))

        # Use a WeakSet here so, even if we fail to discard the connection
        # when it is being closed, or it is closed outside of here, the item
        # will be removed automatically
        self._conns = weakref.WeakSet()
        self._conns_in_progress = 0
        self.maxsize = maxsize
        self.pool = queue.Queue(maxsize=max(reuse, 1))

    def none_if_unusable(self, conn):
        try:
            self.check_usable(conn)
            logger.debug("DB connection reused")
            return conn
        except DatabaseError:
            logger.debug("DB connection was closed")
            return None

    def get(self):
        conn = None

        # Find usable connection in the pool
        while conn is None:
            try:
                conn = self.none_if_unusable(self.pool.get_nowait())
            except queue.Empty:
                break

        # If no usable connection in the pool, and we're at max conns, wait for a
        # usable connection to be put into the pool. To handle the case where conns
        # are _never_ put into the pool, say from failed connection that surface
        # an exception to the user, we check on a loop if we should still wait
        # or fall through to creating a connection
        while conn is None:
            if len(self._conns) + self._conns_in_progress < self.maxsize:
                break
            logger.error('%s out of %s database connections used', len(self._conns) + self._conns_in_progress, self.maxsize)
            try:
                conn = self.none_if_unusable(self.pool.get(timeout=5))
            except queue.Empty:
                pass

        # If still no usable connection, make a new one. In the below, the event loop
        # can only yield at create_connection, so we don't need a lock to avoid race
        # conditions
        if conn is None:
            self._conns_in_progress += 1
            try:
                logger.debug("Creating a new DB connection")
                conn = self.create_connection()
                self._conns.add(conn)
            finally:
                self._conns_in_progress -= 1

        return conn

    def put(self, item):
        try:
            self.pool.put_nowait(item)
            logger.debug("DB connection returned to the pool")
        except queue.Full:
            item.close()
            self._conns.discard(item)

    def closeall(self):
        while not self.pool.empty():
            try:
                conn = self.pool.get_nowait()
            except queue.Empty:
                continue
            try:
                conn.close()
            except Exception:
                continue
            finally:
                self._conns.discard(conn)

        logger.debug("DB connections all closed")


class PostgresConnectionPool(DatabaseConnectionPool):
    def __init__(self, *args, **kwargs):
        self.connect = kwargs.pop('connect', connect)
        self.connection = None
        maxsize = kwargs.pop('MAX_CONNS', 4)
        reuse = kwargs.pop('REUSE_CONNS', maxsize)
        self.args = args
        self.kwargs = kwargs
        super(PostgresConnectionPool, self).__init__(maxsize, reuse)

    def create_connection(self):
        conn = self.connect(*self.args, **self.kwargs)
        # set correct encoding
        conn.set_client_encoding('UTF8')
        psycopg2.extras.register_default_jsonb(conn_or_curs=conn, loads=lambda x: x)
        return conn

    def check_usable(self, connection):
        connection.cursor().execute('SELECT 1')
