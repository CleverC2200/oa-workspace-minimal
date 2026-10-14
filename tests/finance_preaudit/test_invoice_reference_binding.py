from copy import deepcopy

import pytest

from apps.oa_approval import invoice_buyers
from apps.oa_finance_preaudit import case_bundle
from tests.finance_preaudit.test_general_invoice_evidence import (
    CONFIG, RecordedInvoiceApi, linked_invoice, workflow_with_invoices,
)


NUMBERS = {str(index): f'{index:020d}' for index in range(101, 104)}


class RecordedRecords:
    def __init__(self, mutation=None):
        self.calls = []
        self.records = {key: {'data': {'id': 'cloud-' + key, 'number': number,
            'comm_info': {'buyer': {'company': 'Buyer-' + key}, 'seller': {'company': 'Seller-' + key}}}}
            for key, number in NUMBERS.items()}
        if mutation == 'wrong_id': self.records['102']['data']['id'] = 'cloud-other'
        if mutation == 'missing_id': self.records['102']['data'].pop('id')
        if mutation == 'missing_number': self.records['102']['data'].pop('number')
        if mutation == 'foreign_number': self.records['102']['data']['number'] = NUMBERS['103']
        if mutation == 'unreadable': self.records['102'] = {'data': None}

    def get_invoice(self, *, data_id, fid, token):
        assert fid == 'cloud-' + data_id and token == 'synthetic-token'
        self.calls.append(data_id)
        return deepcopy(self.records[data_id])


@pytest.mark.parametrize('order', [('101', '102', '103'), ('103', '101', '102'), ('102', '103', '101')])
@pytest.mark.parametrize('field_style', ['linked', 'invoice_number'])
def test_grouped_references_bind_by_record_identity_and_returned_number(order, field_style):
    field = linked_invoice(data_id='101,102,103', invoice_no=','.join(NUMBERS[key] for key in order))
    if field_style == 'invoice_number': field.update(fieldName='fphm', fieldShowName='发票号码')
    workflow = workflow_with_invoices([[field]])
    client = RecordedRecords()
    result = invoice_buyers.read_invoice_buyers(RecordedInvoiceApi(workflow), workflow,
        invoice_client=client, include_evidence=True)
    assert result['read_status'] == 'content_read'
    assert {row['invoice_no']:row['buyer'] for row in result['invoices']} == {
        number:'Buyer-' + key for key, number in NUMBERS.items()}
    evidence = result['invoice_evidence']['invoices']
    assert {row['data_id']:row['invoice_no'] for row in evidence} == NUMBERS
    assert all(row['fields']['invoice_number']['value'] == NUMBERS[row['data_id']] for row in evidence)
    assert all(not any(key.startswith('_') for key in row) for row in evidence)


@pytest.mark.parametrize('mutation', ['wrong_id', 'missing_id', 'missing_number', 'foreign_number', 'unreadable'])
def test_grouped_references_reject_wrong_incomplete_or_unowned_record(mutation):
    workflow = workflow_with_invoices([[linked_invoice(data_id='101,102',
        invoice_no=','.join(NUMBERS[key] for key in ('101', '102')))]])
    result = invoice_buyers.read_invoice_buyers(RecordedInvoiceApi(workflow), workflow,
        invoice_client=RecordedRecords(mutation))
    assert result['read_status'] == 'failed' and result['fail_code'] == 'invoice_buyer_read_failed'
    assert all(not row.get('buyer') and not row.get('seller') for row in result['invoices'])


def test_grouped_references_cannot_borrow_a_number_from_another_field():
    workflow = workflow_with_invoices([
        [linked_invoice(data_id='101,102', invoice_no=','.join(NUMBERS[key] for key in ('101', '103')))],
        [linked_invoice(data_id='103', invoice_no=NUMBERS['102'])],
    ])
    result = invoice_buyers.read_invoice_buyers(RecordedInvoiceApi(workflow), workflow, invoice_client=RecordedRecords())
    assert result['read_status'] == 'failed'


def test_duplicate_reference_keeps_declared_multiplicity_without_duplicate_reads():
    workflow = workflow_with_invoices([[linked_invoice(data_id='101,101,102',
        invoice_no=','.join(NUMBERS[key] for key in ('102', '101', '101')))]])
    client = RecordedRecords()
    result = invoice_buyers.read_invoice_buyers(RecordedInvoiceApi(workflow), workflow, invoice_client=client)
    assert result['read_status'] == 'content_read' and sorted(client.calls) == ['101', '102']
    assert len(result['invoices']) == 2


def test_duplicate_count_conflict_is_not_repaired_by_set_membership():
    workflow = workflow_with_invoices([[linked_invoice(data_id='101,101,102',
        invoice_no=','.join(NUMBERS[key] for key in ('101', '102', '102')))]])
    result = invoice_buyers.read_invoice_buyers(RecordedInvoiceApi(workflow), workflow, invoice_client=RecordedRecords())
    assert result['read_status'] == 'failed'


@pytest.mark.parametrize('key,value', [('id', True), ('id', []), ('number', False), ('number', {})])
def test_single_reference_rejects_malformed_explicit_identity_and_number(key, value):
    workflow = workflow_with_invoices([[linked_invoice()]])
    client = RecordedRecords()
    client.records['101']['data'][key] = value
    result = invoice_buyers.read_invoice_buyers(RecordedInvoiceApi(workflow), workflow, invoice_client=client)
    assert result['read_status'] == 'failed'


def test_overlapping_groups_merge_paths_without_position_binding():
    workflow = workflow_with_invoices([
        [linked_invoice(data_id='101,102', invoice_no=','.join(NUMBERS[key] for key in ('102', '101')))],
        [linked_invoice(data_id='102,103', invoice_no=','.join(NUMBERS[key] for key in ('103', '102')))],
    ])
    result = invoice_buyers.read_invoice_buyers(RecordedInvoiceApi(workflow), workflow,
        invoice_client=RecordedRecords(), include_evidence=True)
    assert result['read_status'] == 'content_read'
    evidence = result['invoice_evidence']['invoices']
    assert len(next(row for row in evidence if row['data_id'] == '102')['source_field_paths']) == 2


def test_recorded_fourteen_reference_permutation_with_two_singletons(monkeypatch):
    # Retain only the observed ordering, with every ID, number and party synthetic.
    ids = [str(index) for index in range(101, 117)]
    numbers = {key: f'{int(key):020d}' for key in ids}
    order = [0, 1, 2, 3, 5, 6, 8, 4, 9, 7, 10, 11, 12, 13]
    workflow = workflow_with_invoices([
        [linked_invoice(data_id=','.join(ids[:14]), invoice_no=','.join(numbers[ids[index]] for index in order))],
        [linked_invoice(data_id=ids[14], invoice_no=numbers[ids[14]])],
        [linked_invoice(data_id=ids[15], invoice_no=numbers[ids[15]])],
    ])
    client = RecordedRecords()
    client.records = {key: {'data': {'id': 'cloud-' + key, 'number': number,
        'comm_info': {'buyer': {'company': 'Buyer-' + key}, 'seller': {'company': 'Seller-' + key}}}}
        for key, number in numbers.items()}
    monkeypatch.setattr(invoice_buyers, 'InvoiceCloudClient', lambda: client)
    material = case_bundle.collect_case('synthetic-request', oa_api=RecordedInvoiceApi(workflow),
        configs=[CONFIG], allow_browser_download=False)
    assert len(client.calls) == 16
    assert {row['data_id']: row['invoice_no'] for row in material['invoice_evidence']['invoices']} == numbers
    assert {row['subject_ref']: row['value'] for row in material['authority_facts'] if row['name'] == '发票购买方'} == {
        number: 'Buyer-' + key for key, number in numbers.items()}


def test_public_collection_preserves_the_verified_invoice_party_pairs(monkeypatch):
    workflow = workflow_with_invoices([[linked_invoice(data_id='101,102,103',
        invoice_no=','.join(NUMBERS[key] for key in ('103', '101', '102')))]])
    monkeypatch.setattr(invoice_buyers, 'InvoiceCloudClient', RecordedRecords)
    material = case_bundle.collect_case('synthetic-request', oa_api=RecordedInvoiceApi(workflow),
        configs=[CONFIG], allow_browser_download=False)
    assert {row['subject_ref']:row['value'] for row in material['authority_facts'] if row['name']=='发票购买方'} == {
        number:'Buyer-' + key for key, number in NUMBERS.items()}
    assert {row['data_id']:row['invoice_no'] for row in material['invoice_evidence']['invoices']} == NUMBERS
