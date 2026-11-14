"""Draft shipment CSV authority, batch atomicity and resource competition on PG."""
import csv
import io
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests_integration import test_umbrella_workflow as flow


HEADERS = ['external_key', 'line_key', 'shipment_number', 'order_ref', 'reservation_id', 'quantity']


def csv_text(rows, headers=HEADERS):
    result = io.StringIO(newline='')
    writer = csv.writer(result, lineterminator='\n')
    writer.writerow(headers)
    writer.writerows(rows)
    return result.getvalue()


class ShipmentImportsTests(flow.UmbrellaHTTPTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from db import db as d
        from umbrella.businesses import initialize_registry
        d.init_db()
        initialize_registry()

    def reservations(self, portions=(10,), opening=10):
        self.enable()
        demand = self.response(self.owner.post(self.base + f"/logistics/orders/{self.order['id']}/submit",
            json={'idempotency_key': 'shipment-source-order'}))['demands'][0]
        workspace = self.workspace()
        item = self.reference(workspace, 'item', 'Toy Robot')['id']
        location = self.reference(workspace, 'location', 'Toy Warehouse')['id']
        self.command('open_stock', {'item_id': item, 'location_id': location, 'quantity': opening,
            'source_ref': 'Shipment import physical opening count'}, 'shipment-opening')
        reservations = []
        for index, quantity in enumerate(portions):
            reservations.append(self.command('reserve', {'item_id': item, 'location_id': location,
                'quantity': quantity, 'order_ref': demand['external_key'], 'demand_line_id': demand['id']},
                'shipment-reservation-' + str(index))['reservation_id'])
        return demand, reservations, item, location

    def preview(self, text):
        return self.response(self.owner.post(self.base + '/logistics/imports/shipments/preview', json={'csv_text': text}))

    def commit(self, text, preview, key, expected=200):
        return self.response(self.owner.post(self.base + '/logistics/imports/shipments/commit', json={
            'csv_text': text, 'base_version': preview['base_version'], 'digest': preview['digest'], 'idempotency_key': key}), expected)

    def test_draft_import_is_read_only_before_approval_and_deduplicates_after_dispatch(self):
        demand, reservations, _, _ = self.reservations((6, 4))
        template = self.owner.get(self.base + '/logistics/imports/shipments/template')
        self.assertEqual(template.status_code, 200)
        self.assertEqual(template.text, ','.join(HEADERS) + '\n')
        rows = [['ship-1', 'A', 'CSV-SHIP-1', demand['order_ref'], reservations[0], 6],
                ['ship-1', 'B', 'CSV-SHIP-1', demand['order_ref'], reservations[1], 4]]
        text = csv_text(rows)
        before = self.fixture.core_snapshot(self.bid)
        count = self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.command_requests')
        preview = self.preview(text)
        self.assertTrue(preview['summary']['valid'], preview['errors'])
        self.assertEqual((preview['summary']['total_rows'], preview['summary']['total_shipments']), (2, 1))
        self.assertEqual(self.fixture.core_snapshot(self.bid), before)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.command_requests'), count)
        self.assertIsNone(self.fixture.scalar(self.bid, "SELECT to_regclass('logistics.host_imports')"))
        result = self.commit(text, preview, 'shipment-csv-once')
        self.assertEqual((result['created_count'], result['skipped_count']), (1, 0))
        self.assertEqual(len(result['created'][0]['line_ids']), 2)
        self.assertEqual(result['created'][0]['status'], 'draft')
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.shipment_dispatches'), 0)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.stock_movements'), 1)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT sum(on_hand) FROM logistics.stock_levels'), 10)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT sum(reserved) FROM logistics.stock_levels'), 10)
        self.assertEqual(self.commit(text, preview, 'shipment-csv-once'), result)
        reordered = csv_text(reversed(rows))
        again = self.preview(reordered)
        self.assertTrue(again['summary']['valid'], again['errors'])
        self.assertEqual(self.commit(reordered, again, 'same-shipment-new-key')['skipped_count'], 1)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.shipments'), 1)
        shipment = result['created'][0]['shipment_id']
        self.command('dispatch', {'shipment_id': shipment, 'carrier': 'Manual carrier', 'tracking_ref': 'TRACK-CSV',
            'as_of': self.today.isoformat()}, 'explicit-dispatch')
        movements = self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.stock_movements')
        after_dispatch = self.preview(text)
        self.assertTrue(after_dispatch['summary']['valid'], after_dispatch['errors'])
        self.assertEqual(self.commit(text, after_dispatch, 'same-shipment-after-dispatch')['skipped_count'], 1)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.stock_movements'), movements)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.shipment_dispatches'), 1)

    def test_malformed_duplicate_and_typed_reference_errors_have_rows(self):
        demand, reservations, _, _ = self.reservations()
        good = ['ship-1', 'A', 'CSV-SHIP-1', demand['order_ref'], reservations[0], 2]
        for index, value, field in ((4, '1.5', 'reservation_id'), (5, '1e3', 'quantity'),
                (5, '1,5', 'quantity'), (4, 999999, 'reservation_id'),
                (3, f"core:{self.gym['id']}:crm_other:1", 'reservation_id')):
            row = list(good)
            row[index] = value
            preview = self.preview(csv_text([row]))
            self.assertFalse(preview['summary']['valid'])
            self.assertTrue(any(error['row'] == 2 and error['field'] == field for error in preview['errors']), preview['errors'])
        duplicate = self.preview(csv_text([good, good]))
        self.assertFalse(duplicate['summary']['valid'])
        self.assertTrue(any(error['field'] == 'line_key' and error['row'] == 3 for error in duplicate['errors']))
        extra = self.preview(csv_text([good + ['carrier']], HEADERS + ['carrier']))
        self.assertFalse(extra['summary']['valid'])
        self.assertTrue(any(error['field'] == 'headers' for error in extra['errors']))
        self.response(self.stranger.post(self.base + '/logistics/imports/shipments/preview', json={'csv_text': csv_text([good])}), 404)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.shipments'), 0)
        from umbrella.shipment_imports import _read
        _, errors = _read('shipments', csv_text([good]))
        self.assertFalse(errors)
        _, errors = _read('shipments', ','.join(HEADERS) + '\n' + (','.join(map(str, good)) + '\n') * 5001)
        self.assertTrue(any(error['field'] == 'rows' for error in errors))

    def test_one_shipment_cannot_mix_customer_orders(self):
        demand, reservations, item, location = self.reservations(opening=20)
        other_order = self.record('Customer order', {'Order number': 'OTHER-ORDER', 'Currency': 'USD'}, {'Customer': self.customer['id']})
        self.record('Order line', {'Quantity': 3, 'Unit price (minor units)': 1000, 'Required by': self.deadline},
            {'Order': other_order['id'], 'Item': self.item['id']})
        other = self.response(self.owner.post(self.base + f"/logistics/orders/{other_order['id']}/submit",
            json={'idempotency_key': 'other-order'}))['demands'][0]
        reserved = self.command('reserve', {'item_id': item, 'location_id': location, 'quantity': 3,
            'order_ref': other['external_key'], 'demand_line_id': other['id']}, 'other-reservation')['reservation_id']
        text = csv_text([['mixed', 'A', 'MIXED', demand['order_ref'], reservations[0], 2],
                         ['mixed', 'B', 'MIXED', demand['order_ref'], reserved, 3]])
        preview = self.preview(text)
        self.assertFalse(preview['summary']['valid'])
        self.assertTrue(any(error['row'] == 3 and 'different customer order' in error['message'] for error in preview['errors']))
        self.commit(text, preview, 'mixed-orders', 422)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.shipments'), 0)

    def test_later_line_failure_rolls_back_entire_file_and_key(self):
        demand, reservations, _, _ = self.reservations()
        text = csv_text([['group-A', 'one', 'ATOMIC-A', demand['order_ref'], reservations[0], 3],
                         ['group-B', 'one', 'ATOMIC-B', demand['order_ref'], reservations[0], 3]])
        preview = self.preview(text)
        self.assertTrue(preview['summary']['valid'], preview['errors'])
        from umbrella import shipment_imports
        original, calls = shipment_imports._insert_line, 0

        def fail_later(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('Injected second shipment line failure')
            return original(*args, **kwargs)

        with patch.object(shipment_imports, '_insert_line', side_effect=fail_later):
            self.commit(text, preview, 'atomic-shipment-file', 503)
        for table in ('shipments', 'shipment_lines', 'shipment_dispatches'):
            self.assertEqual(self.fixture.scalar(self.bid, f'SELECT count(*) FROM logistics.{table}'), 0)
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.command_requests WHERE idempotency_key='atomic-shipment-file'"), 0)
        self.assertIsNone(self.fixture.scalar(self.bid, "SELECT to_regclass('logistics.host_imports')"))
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.stock_movements'), 1)
        result = self.commit(text, preview, 'atomic-shipment-file')
        self.assertEqual(result['created_count'], 2)

    def test_remaining_capacity_is_rechecked_at_commit_and_content_cannot_change(self):
        demand, reservations, _, _ = self.reservations()
        text = csv_text([['planned', 'one', 'PLANNED', demand['order_ref'], reservations[0], 8]])
        preview = self.preview(text)
        self.assertTrue(preview['summary']['valid'], preview['errors'])
        draft = self.command('create_shipment', {'shipment_number': 'MANUAL', 'order_ref': demand['order_ref']})
        self.command('add_shipment_line', {'shipment_id': draft['shipment_id'], 'reservation_id': reservations[0], 'quantity': 4})
        self.commit(text, preview, 'capacity-rechecked', 422)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.shipments'), 1)
        valid = csv_text([['planned', 'one', 'PLANNED', demand['order_ref'], reservations[0], 6]])
        accepted = self.preview(valid)
        self.assertTrue(accepted['summary']['valid'], accepted['errors'])
        self.commit(valid, accepted, 'planned-six')
        changed = csv_text([['planned', 'one', 'PLANNED', demand['order_ref'], reservations[0], 5]])
        different = self.preview(changed)
        self.assertFalse(different['summary']['valid'])
        self.assertTrue(any(error['field'] == 'external_key' for error in different['errors']))
        self.commit(changed, different, 'changed-import', 422)
        self.commit(changed, different, 'planned-six', 409)

    def test_concurrent_batches_cannot_both_plan_last_reserved_units(self):
        demand, reservations, _, _ = self.reservations()
        batches = []
        for name in ('A', 'B'):
            text = csv_text([[name, 'one', 'RACE-' + name, demand['order_ref'], reservations[0], 10]])
            preview = self.preview(text)
            self.assertTrue(preview['summary']['valid'], preview['errors'])
            batches.append((name, text, preview))
        from fastapi.testclient import TestClient
        cookies = dict(self.owner.cookies)

        def commit(batch):
            name, text, preview = batch
            with TestClient(self.app, base_url='https://testserver') as client:
                client.cookies.update(cookies)
                return client.post(self.base + '/logistics/imports/shipments/commit', json={
                    'csv_text': text, 'base_version': preview['base_version'], 'digest': preview['digest'],
                    'idempotency_key': 'race-shipment-' + name}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(commit, batches))
        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertEqual(sum(status in (409, 422) for status in statuses), 1, statuses)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.shipments'), 1)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT sum(quantity) FROM logistics.shipment_lines'), 10)
        self.assertEqual(self.fixture.scalar(self.bid, 'SELECT count(*) FROM logistics.stock_movements'), 1)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ShipmentImportsTests(name) for name, method in ShipmentImportsTests.__dict__.items()
        if name.startswith('test_') and callable(method))


if __name__ == '__main__':
    unittest.main()
