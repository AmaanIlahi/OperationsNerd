"""OperationsNerd's purchasing, immutable stock and fulfillment capability.

The host owns authentication, authoritative records and database routing. This
package contains no standalone setup routes, owner tokens, or environment reads.
Public commands require an idle autocommit psycopg connection configured by
``db.prepare_connection``; purchasing and stock share one request registry.
"""
