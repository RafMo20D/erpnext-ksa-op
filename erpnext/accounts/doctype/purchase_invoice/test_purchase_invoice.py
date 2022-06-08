# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


import unittest

import frappe
from frappe.tests.utils import change_settings
from frappe.utils import add_days, cint, flt, getdate, nowdate, today

import erpnext
from erpnext.accounts.doctype.account.test_account import create_account, get_inventory_account
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.buying.doctype.purchase_order.purchase_order import get_mapped_purchase_invoice
from erpnext.buying.doctype.purchase_order.test_purchase_order import create_purchase_order
from erpnext.buying.doctype.supplier.test_supplier import create_supplier
from erpnext.controllers.accounts_controller import get_payment_terms
from erpnext.controllers.buying_controller import QtyMismatchError
from erpnext.exceptions import InvalidCurrency
from erpnext.projects.doctype.project.test_project import make_project
from erpnext.stock.doctype.item.test_item import create_item
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
	make_purchase_invoice as create_purchase_invoice_from_receipt,
)
from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import (
	get_taxes,
	make_purchase_receipt,
)
from erpnext.stock.doctype.stock_entry.test_stock_entry import get_qty_after_transaction
from erpnext.stock.tests.test_utils import StockTestMixin

test_dependencies = ["Item", "Cost Center", "Payment Term", "Payment Terms Template"]
test_ignore = ["Serial No"]


class TestPurchaseInvoice(unittest.TestCase, StockTestMixin):
	@classmethod
	def setUpClass(self):
		unlink_payment_on_cancel_of_invoice()
		frappe.db.set_value("Buying Settings", None, "allow_multiple_items", 1)

	@classmethod
	def tearDownClass(self):
		unlink_payment_on_cancel_of_invoice(0)

	def test_purchase_invoice_received_qty(self):
		"""
		1. Test if received qty is validated against accepted + rejected
		2. Test if received qty is auto set on save
		"""
		pi = make_purchase_invoice(
			qty=1,
			rejected_qty=1,
			received_qty=3,
			item_code="_Test Item Home Desktop 200",
			rejected_warehouse="_Test Rejected Warehouse - _TC",
			update_stock=True,
			do_not_save=True,
		)
		self.assertRaises(QtyMismatchError, pi.save)

		pi.items[0].received_qty = 0
		pi.save()
		self.assertEqual(pi.items[0].received_qty, 2)

		# teardown
		pi.delete()

	def test_gl_entries_without_perpetual_inventory(self):
		frappe.db.set_value("Company", "_Test Company", "round_off_account", "Round Off - _TC")
		pi = frappe.copy_doc(test_records[0])
		self.assertTrue(not cint(erpnext.is_perpetual_inventory_enabled(pi.company)))
		pi.insert()
		pi.submit()

		expected_gl_entries = {
			"_Test Payable - _TC": [0, 1512.0],
			"_Test Account Cost for Goods Sold - _TC": [1250, 0],
			"_Test Account Shipping Charges - _TC": [100, 0],
			"_Test Account Excise Duty - _TC": [140, 0],
			"_Test Account Education Cess - _TC": [2.8, 0],
			"_Test Account S&H Education Cess - _TC": [1.4, 0],
			"_Test Account CST - _TC": [29.88, 0],
			"_Test Account VAT - _TC": [156.25, 0],
			"_Test Account Discount - _TC": [0, 168.03],
			"Round Off - _TC": [0, 0.3],
		}
		gl_entries = frappe.db.sql(
			"""select account, debit, credit from `tabGL Entry`
			where voucher_type = 'Purchase Invoice' and voucher_no = %s""",
			pi.name,
			as_dict=1,
		)
		for d in gl_entries:
			self.assertEqual([d.debit, d.credit], expected_gl_entries.get(d.account))

	def test_gl_entries_with_perpetual_inventory(self):
		pi = make_purchase_invoice(
			company="_Test Company with perpetual inventory",
			warehouse="Stores - TCP1",
			cost_center="Main - TCP1",
			expense_account="_Test Account Cost for Goods Sold - TCP1",
			get_taxes_and_charges=True,
			qty=10,
		)

		self.assertTrue(cint(erpnext.is_perpetual_inventory_enabled(pi.company)), 1)

		self.check_gle_for_pi(pi.name)

	def test_terms_added_after_save(self):
		pi = frappe.copy_doc(test_records[1])
		pi.insert()
		self.assertTrue(pi.payment_schedule)
		self.assertEqual(pi.payment_schedule[0].due_date, pi.due_date)

	def test_payment_entry_unlink_against_purchase_invoice(self):
		from erpnext.accounts.doctype.payment_entry.test_payment_entry import get_payment_entry

		unlink_payment_on_cancel_of_invoice(0)

		pi_doc = make_purchase_invoice()

		pe = get_payment_entry("Purchase Invoice", pi_doc.name, bank_account="_Test Bank - _TC")
		pe.reference_no = "1"
		pe.reference_date = nowdate()
		pe.paid_from_account_currency = pi_doc.currency
		pe.paid_to_account_currency = pi_doc.currency
		pe.source_exchange_rate = 1
		pe.target_exchange_rate = 1
		pe.paid_amount = pi_doc.grand_total
		pe.save(ignore_permissions=True)
		pe.submit()

		pi_doc = frappe.get_doc("Purchase Invoice", pi_doc.name)
		pi_doc.load_from_db()
		self.assertTrue(pi_doc.status, "Paid")

		self.assertRaises(frappe.LinkExistsError, pi_doc.cancel)
		unlink_payment_on_cancel_of_invoice()

	def test_purchase_invoice_for_blocked_supplier(self):
		supplier = frappe.get_doc("Supplier", "_Test Supplier")
		supplier.on_hold = 1
		supplier.save()

		self.assertRaises(frappe.ValidationError, make_purchase_invoice)

		supplier.on_hold = 0
		supplier.save()

	def test_purchase_invoice_for_blocked_supplier_invoice(self):
		supplier = frappe.get_doc("Supplier", "_Test Supplier")
		supplier.on_hold = 1
		supplier.hold_type = "Invoices"
		supplier.save()

		self.assertRaises(frappe.ValidationError, make_purchase_invoice)

		supplier.on_hold = 0
		supplier.save()

	def test_purchase_invoice_for_blocked_supplier_payment(self):
		supplier = frappe.get_doc("Supplier", "_Test Supplier")
		supplier.on_hold = 1
		supplier.hold_type = "Payments"
		supplier.save()

		pi = make_purchase_invoice()

		self.assertRaises(
			frappe.ValidationError,
			get_payment_entry,
			dt="Purchase Invoice",
			dn=pi.name,
			bank_account="_Test Bank - _TC",
		)

		supplier.on_hold = 0
		supplier.save()

	def test_purchase_invoice_for_blocked_supplier_payment_today_date(self):
		supplier = frappe.get_doc("Supplier", "_Test Supplier")
		supplier.on_hold = 1
		supplier.hold_type = "Payments"
		supplier.release_date = nowdate()
		supplier.save()

		pi = make_purchase_invoice()

		self.assertRaises(
			frappe.ValidationError,
			get_payment_entry,
			dt="Purchase Invoice",
			dn=pi.name,
			bank_account="_Test Bank - _TC",
		)

		supplier.on_hold = 0
		supplier.save()

	def test_purchase_invoice_for_blocked_supplier_payment_past_date(self):
		# this test is meant to fail only if something fails in the try block
		with self.assertRaises(Exception):
			try:
				supplier = frappe.get_doc("Supplier", "_Test Supplier")
				supplier.on_hold = 1
				supplier.hold_type = "Payments"
				supplier.release_date = "2018-03-01"
				supplier.save()

				pi = make_purchase_invoice()

				get_payment_entry("Purchase Invoice", dn=pi.name, bank_account="_Test Bank - _TC")

				supplier.on_hold = 0
				supplier.save()
			except:
				pass
			else:
				raise Exception

	def test_purchase_invoice_blocked_invoice_must_be_in_future(self):
		pi = make_purchase_invoice(do_not_save=True)
		pi.release_date = nowdate()

		self.assertRaises(frappe.ValidationError, pi.save)
		pi.release_date = ""
		pi.save()

	def test_purchase_invoice_temporary_blocked(self):
		pi = make_purchase_invoice(do_not_save=True)
		pi.release_date = add_days(nowdate(), 10)
		pi.save()
		pi.submit()

		pe = get_payment_entry("Purchase Invoice", dn=pi.name, bank_account="_Test Bank - _TC")

		self.assertRaises(frappe.ValidationError, pe.save)

	def test_purchase_invoice_explicit_block(self):
		pi = make_purchase_invoice()
		pi.block_invoice()

		self.assertEqual(pi.on_hold, 1)

		pi.unblock_invoice()

		self.assertEqual(pi.on_hold, 0)

	def test_gl_entries_with_perpetual_inventory_against_pr(self):

		pr = make_purchase_receipt(
			company="_Test Company with perpetual inventory",
			supplier_warehouse="Work In Progress - TCP1",
			warehouse="Stores - TCP1",
			cost_center="Main - TCP1",
			get_taxes_and_charges=True,
		)

		pi = make_purchase_invoice(
			company="_Test Company with perpetual inventory",
			supplier_warehouse="Work In Progress - TCP1",
			warehouse="Stores - TCP1",
			cost_center="Main - TCP1",
			expense_account="_Test Account Cost for Goods Sold - TCP1",
			get_taxes_and_charges=True,
			qty=10,
			do_not_save="True",
		)

		for d in pi.items:
			d.purchase_receipt = pr.name

		pi.insert()
		pi.submit()
		pi.load_from_db()

		self.assertTrue(pi.status, "Unpaid")
		self.check_gle_for_pi(pi.name)

	def check_gle_for_pi(self, pi):
		gl_entries = frappe.db.sql(
			"""select account, sum(debit) as debit, sum(credit) as credit
			from `tabGL Entry` where voucher_type='Purchase Invoice' and voucher_no=%s
			group by account""",
			pi,
			as_dict=1,
		)

		self.assertTrue(gl_entries)

		expected_values = dict(
			(d[0], d)
			for d in [
				["Creditors - TCP1", 0, 720],
				["Stock Received But Not Billed - TCP1", 500.0, 0],
				["_Test Account Shipping Charges - TCP1", 100.0, 0.0],
				["_Test Account VAT - TCP1", 120.0, 0],
			]
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_values[gle.account][0], gle.account)
			self.assertEqual(expected_values[gle.account][1], gle.debit)
			self.assertEqual(expected_values[gle.account][2], gle.credit)

	def test_purchase_invoice_with_exchange_rate_difference(self):
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
			make_purchase_invoice as create_purchase_invoice,
		)

		pr = make_purchase_receipt(
			company="_Test Company with perpetual inventory",
			warehouse="Stores - TCP1",
			currency="USD",
			conversion_rate=70,
		)

		pi = create_purchase_invoice(pr.name)
		pi.conversion_rate = 80

		pi.insert()
		pi.submit()

		# Get exchnage gain and loss account
		exchange_gain_loss_account = frappe.db.get_value(
			"Company", pi.company, "exchange_gain_loss_account"
		)

		# fetching the latest GL Entry with exchange gain and loss account account
		amount = frappe.db.get_value(
			"GL Entry", {"account": exchange_gain_loss_account, "voucher_no": pi.name}, "debit"
		)
		discrepancy_caused_by_exchange_rate_diff = abs(
			pi.items[0].base_net_amount - pr.items[0].base_net_amount
		)

		self.assertEqual(discrepancy_caused_by_exchange_rate_diff, amount)

	@change_settings("Buying Settings", {"enable_discount_accounting": 1})
	def test_purchase_invoice_with_discount_accounting_enabled(self):

		discount_account = create_account(
			account_name="Discount Account",
			parent_account="Indirect Expenses - _TC",
			company="_Test Company",
		)
		pi = make_purchase_invoice(discount_account=discount_account, rate=45)

		expected_gle = [
			["_Test Account Cost for Goods Sold - _TC", 250.0, 0.0, nowdate()],
			["Creditors - _TC", 0.0, 225.0, nowdate()],
			["Discount Account - _TC", 0.0, 25.0, nowdate()],
		]

		check_gl_entries(self, pi.name, expected_gle, nowdate())

	@change_settings("Buying Settings", {"enable_discount_accounting": 1})
	def test_additional_discount_for_purchase_invoice_with_discount_accounting_enabled(self):

		additional_discount_account = create_account(
			account_name="Discount Account",
			parent_account="Indirect Expenses - _TC",
			company="_Test Company",
		)

		pi = make_purchase_invoice(do_not_save=1, parent_cost_center="Main - _TC")
		pi.apply_discount_on = "Grand Total"
		pi.additional_discount_account = additional_discount_account
		pi.additional_discount_percentage = 10
		pi.disable_rounded_total = 1
		pi.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": "_Test Account VAT - _TC",
				"cost_center": "Main - _TC",
				"description": "Test",
				"rate": 10,
			},
		)
		pi.submit()

		expected_gle = [
			["_Test Account Cost for Goods Sold - _TC", 250.0, 0.0, nowdate()],
			["_Test Account VAT - _TC", 25.0, 0.0, nowdate()],
			["Creditors - _TC", 0.0, 247.5, nowdate()],
			["Discount Account - _TC", 0.0, 27.5, nowdate()],
		]

		check_gl_entries(self, pi.name, expected_gle, nowdate())

	def test_purchase_invoice_change_naming_series(self):
		pi = frappe.copy_doc(test_records[1])
		pi.insert()
		pi.naming_series = "TEST-"

		self.assertRaises(frappe.CannotChangeConstantError, pi.save)

		pi = frappe.copy_doc(test_records[0])
		pi.insert()
		pi.load_from_db()

		self.assertTrue(pi.status, "Draft")
		pi.naming_series = "TEST-"

		self.assertRaises(frappe.CannotChangeConstantError, pi.save)

	def test_gl_entries_for_non_stock_items_with_perpetual_inventory(self):
		pi = make_purchase_invoice(
			item_code="_Test Non Stock Item",
			company="_Test Company with perpetual inventory",
			warehouse="Stores - TCP1",
			cost_center="Main - TCP1",
			expense_account="_Test Account Cost for Goods Sold - TCP1",
		)

		self.assertTrue(pi.status, "Unpaid")

		gl_entries = frappe.db.sql(
			"""select account, debit, credit
			from `tabGL Entry` where voucher_type='Purchase Invoice' and voucher_no=%s
			order by account asc""",
			pi.name,
			as_dict=1,
		)
		self.assertTrue(gl_entries)

		expected_values = [
			["_Test Account Cost for Goods Sold - TCP1", 250.0, 0],
			["Creditors - TCP1", 0, 250],
		]

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_values[i][0], gle.account)
			self.assertEqual(expected_values[i][1], gle.debit)
			self.assertEqual(expected_values[i][2], gle.credit)

	def test_purchase_invoice_calculation(self):
		pi = frappe.copy_doc(test_records[0])
		pi.insert()
		pi.load_from_db()

		expected_values = [
			["_Test Item Home Desktop 100", 90, 59],
			["_Test Item Home Desktop 200", 135, 177],
		]
		for i, item in enumerate(pi.get("items")):
			self.assertEqual(item.item_code, expected_values[i][0])
			self.assertEqual(item.item_tax_amount, expected_values[i][1])
			self.assertEqual(item.valuation_rate, expected_values[i][2])

		self.assertEqual(pi.base_net_total, 1250)

		# tax amounts
		expected_values = [
			["_Test Account Shipping Charges - _TC", 100, 1350],
			["_Test Account Customs Duty - _TC", 125, 1350],
			["_Test Account Excise Duty - _TC", 140, 1490],
			["_Test Account Education Cess - _TC", 2.8, 1492.8],
			["_Test Account S&H Education Cess - _TC", 1.4, 1494.2],
			["_Test Account CST - _TC", 29.88, 1524.08],
			["_Test Account VAT - _TC", 156.25, 1680.33],
			["_Test Account Discount - _TC", 168.03, 1512.30],
		]

		for i, tax in enumerate(pi.get("taxes")):
			self.assertEqual(tax.account_head, expected_values[i][0])
			self.assertEqual(tax.tax_amount, expected_values[i][1])
			self.assertEqual(tax.total, expected_values[i][2])

	def test_purchase_invoice_with_subcontracted_item(self):
		wrapper = frappe.copy_doc(test_records[0])
		wrapper.get("items")[0].item_code = "_Test FG Item"
		wrapper.insert()
		wrapper.load_from_db()

		expected_values = [["_Test FG Item", 90, 59], ["_Test Item Home Desktop 200", 135, 177]]
		for i, item in enumerate(wrapper.get("items")):
			self.assertEqual(item.item_code, expected_values[i][0])
			self.assertEqual(item.item_tax_amount, expected_values[i][1])
			self.assertEqual(item.valuation_rate, expected_values[i][2])

		self.assertEqual(wrapper.base_net_total, 1250)

		# tax amounts
		expected_values = [
			["_Test Account Shipping Charges - _TC", 100, 1350],
			["_Test Account Customs Duty - _TC", 125, 1350],
			["_Test Account Excise Duty - _TC", 140, 1490],
			["_Test Account Education Cess - _TC", 2.8, 1492.8],
			["_Test Account S&H Education Cess - _TC", 1.4, 1494.2],
			["_Test Account CST - _TC", 29.88, 1524.08],
			["_Test Account VAT - _TC", 156.25, 1680.33],
			["_Test Account Discount - _TC", 168.03, 1512.30],
		]

		for i, tax in enumerate(wrapper.get("taxes")):
			self.assertEqual(tax.account_head, expected_values[i][0])
			self.assertEqual(tax.tax_amount, expected_values[i][1])
			self.assertEqual(tax.total, expected_values[i][2])

	def test_purchase_invoice_with_advance(self):
		from erpnext.accounts.doctype.journal_entry.test_journal_entry import (
			test_records as jv_test_records,
		)

		jv = frappe.copy_doc(jv_test_records[1])
		jv.insert()
		jv.submit()

		pi = frappe.copy_doc(test_records[0])
		pi.disable_rounded_total = 1
		pi.allocate_advances_automatically = 0
		pi.append(
			"advances",
			{
				"reference_type": "Journal Entry",
				"reference_name": jv.name,
				"reference_row": jv.get("accounts")[0].name,
				"advance_amount": 400,
				"allocated_amount": 300,
				"remarks": jv.remark,
			},
		)
		pi.insert()

		self.assertEqual(pi.outstanding_amount, 1212.30)

		pi.disable_rounded_total = 0
		pi.get("payment_schedule")[0].payment_amount = 1512.0
		pi.save()
		self.assertEqual(pi.outstanding_amount, 1212.0)

		pi.submit()
		pi.load_from_db()

		self.assertTrue(
			frappe.db.sql(
				"""select name from `tabJournal Entry Account`
			where reference_type='Purchase Invoice'
			and reference_name=%s and debit_in_account_currency=300""",
				pi.name,
			)
		)

		pi.cancel()

		self.assertFalse(
			frappe.db.sql(
				"""select name from `tabJournal Entry Account`
			where reference_type='Purchase Invoice' and reference_name=%s""",
				pi.name,
			)
		)

	def test_invoice_with_advance_and_multi_payment_terms(self):
		from erpnext.accounts.doctype.journal_entry.test_journal_entry import (
			test_records as jv_test_records,
		)

		jv = frappe.copy_doc(jv_test_records[1])
		jv.insert()
		jv.submit()

		pi = frappe.copy_doc(test_records[0])
		pi.disable_rounded_total = 1
		pi.allocate_advances_automatically = 0
		pi.append(
			"advances",
			{
				"reference_type": "Journal Entry",
				"reference_name": jv.name,
				"reference_row": jv.get("accounts")[0].name,
				"advance_amount": 400,
				"allocated_amount": 300,
				"remarks": jv.remark,
			},
		)
		pi.insert()

		pi.update(
			{
				"payment_schedule": get_payment_terms(
					"_Test Payment Term Template", pi.posting_date, pi.grand_total, pi.base_grand_total
				)
			}
		)

		pi.save()
		pi.submit()
		self.assertEqual(pi.payment_schedule[0].payment_amount, 606.15)
		self.assertEqual(pi.payment_schedule[0].due_date, pi.posting_date)
		self.assertEqual(pi.payment_schedule[1].payment_amount, 606.15)
		self.assertEqual(pi.payment_schedule[1].due_date, add_days(pi.posting_date, 30))

		pi.load_from_db()

		self.assertTrue(
			frappe.db.sql(
				"select name from `tabJournal Entry Account` where reference_type='Purchase Invoice' and "
				"reference_name=%s and debit_in_account_currency=300",
				pi.name,
			)
		)

		self.assertEqual(pi.outstanding_amount, 1212.30)

		pi.cancel()

		self.assertFalse(
			frappe.db.sql(
				"select name from `tabJournal Entry Account` where reference_type='Purchase Invoice' and "
				"reference_name=%s",
				pi.name,
			)
		)

	def test_total_purchase_cost_for_project(self):
		if not frappe.db.exists("Project", {"project_name": "_Test Project for Purchase"}):
			project = make_project({"project_name": "_Test Project for Purchase"})
		else:
			project = frappe.get_doc("Project", {"project_name": "_Test Project for Purchase"})

		existing_purchase_cost = frappe.db.sql(
			"""select sum(base_net_amount)
			from `tabPurchase Invoice Item`
			where project = '{0}'
			and docstatus=1""".format(
				project.name
			)
		)
		existing_purchase_cost = existing_purchase_cost and existing_purchase_cost[0][0] or 0

		pi = make_purchase_invoice(currency="USD", conversion_rate=60, project=project.name)
		self.assertEqual(
			frappe.db.get_value("Project", project.name, "total_purchase_cost"),
			existing_purchase_cost + 15000,
		)

		pi1 = make_purchase_invoice(qty=10, project=project.name)
		self.assertEqual(
			frappe.db.get_value("Project", project.name, "total_purchase_cost"),
			existing_purchase_cost + 15500,
		)

		pi1.cancel()
		self.assertEqual(
			frappe.db.get_value("Project", project.name, "total_purchase_cost"),
			existing_purchase_cost + 15000,
		)

		pi.cancel()
		self.assertEqual(
			frappe.db.get_value("Project", project.name, "total_purchase_cost"), existing_purchase_cost
		)

	def test_return_purchase_invoice_with_perpetual_inventory(self):
		pi = make_purchase_invoice(
			company="_Test Company with perpetual inventory",
			warehouse="Stores - TCP1",
			cost_center="Main - TCP1",
			expense_account="_Test Account Cost for Goods Sold - TCP1",
		)

		return_pi = make_purchase_invoice(
			is_return=1,
			return_against=pi.name,
			qty=-2,
			company="_Test Company with perpetual inventory",
			warehouse="Stores - TCP1",
			cost_center="Main - TCP1",
			expense_account="_Test Account Cost for Goods Sold - TCP1",
		)

		# check gl entries for return
		gl_entries = frappe.db.sql(
			"""select account, debit, credit
			from `tabGL Entry` where voucher_type=%s and voucher_no=%s
			order by account desc""",
			("Purchase Invoice", return_pi.name),
			as_dict=1,
		)

		self.assertTrue(gl_entries)

		expected_values = {
			"Creditors - TCP1": [100.0, 0.0],
			"Stock Received But Not Billed - TCP1": [0.0, 100.0],
		}

		for gle in gl_entries:
			self.assertEqual(expected_values[gle.account][0], gle.debit)
			self.assertEqual(expected_values[gle.account][1], gle.credit)

	def test_standalone_return_using_pi(self):
		from erpnext.stock.doctype.stock_entry.test_stock_entry import make_stock_entry

		item = self.make_item().name
		company = "_Test Company with perpetual inventory"
		warehouse = "Stores - TCP1"

		make_stock_entry(item_code=item, target=warehouse, qty=50, rate=120)

		return_pi = make_purchase_invoice(
			is_return=1,
			item=item,
			qty=-10,
			update_stock=1,
			rate=100,
			company=company,
			warehouse=warehouse,
			cost_center="Main - TCP1",
		)

		# assert that stock consumption is with actual rate
		self.assertGLEs(
			return_pi,
			[{"credit": 1200, "debit": 0}],
			gle_filters={"account": "Stock In Hand - TCP1"},
		)

		# assert loss booked in COGS
		self.assertGLEs(
			return_pi,
			[{"credit": 0, "debit": 200}],
			gle_filters={"account": "Cost of Goods Sold - TCP1"},
		)

	def test_return_with_lcv(self):
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		from erpnext.stock.doctype.landed_cost_voucher.test_landed_cost_voucher import (
			create_landed_cost_voucher,
		)

		item = self.make_item().name
		company = "_Test Company with perpetual inventory"
		warehouse = "Stores - TCP1"
		cost_center = "Main - TCP1"

		pi = make_purchase_invoice(
			item=item,
			company=company,
			warehouse=warehouse,
			cost_center=cost_center,
			update_stock=1,
			qty=10,
			rate=100,
		)

		# Create landed cost voucher - will increase valuation of received item by 10
		create_landed_cost_voucher("Purchase Invoice", pi.name, pi.company, charges=100)
		return_pi = make_return_doc(pi.doctype, pi.name)
		return_pi.save().submit()

		# assert that stock consumption is with actual in rate
		self.assertGLEs(
			return_pi,
			[{"credit": 1100, "debit": 0}],
			gle_filters={"account": "Stock In Hand - TCP1"},
		)

		# assert loss booked in COGS
		self.assertGLEs(
			return_pi,
			[{"credit": 0, "debit": 100}],
			gle_filters={"account": "Cost of Goods Sold - TCP1"},
		)

	def test_multi_currency_gle(self):
		pi = make_purchase_invoice(
			supplier="_Test Supplier USD",
			credit_to="_Test Payable USD - _TC",
			currency="USD",
			conversion_rate=50,
		)

		gl_entries = frappe.db.sql(
			"""select account, account_currency, debit, credit,
			debit_in_account_currency, credit_in_account_currency
			from `tabGL Entry` where voucher_type='Purchase Invoice' and voucher_no=%s
			order by account asc""",
			pi.name,
			as_dict=1,
		)

		self.assertTrue(gl_entries)

		expected_values = {
			"_Test Payable USD - _TC": {
				"account_currency": "USD",
				"debit": 0,
				"debit_in_account_currency": 0,
				"credit": 12500,
				"credit_in_account_currency": 250,
			},
			"_Test Account Cost for Goods Sold - _TC": {
				"account_currency": "INR",
				"debit": 12500,
				"debit_in_account_currency": 12500,
				"credit": 0,
				"credit_in_account_currency": 0,
			},
		}

		for field in (
			"account_currency",
			"debit",
			"debit_in_account_currency",
			"credit",
			"credit_in_account_currency",
		):
			for i, gle in enumerate(gl_entries):
				self.assertEqual(expected_values[gle.account][field], gle[field])

		# Check for valid currency
		pi1 = make_purchase_invoice(
			supplier="_Test Supplier USD", credit_to="_Test Payable USD - _TC", do_not_save=True
		)

		self.assertRaises(InvalidCurrency, pi1.save)

		# cancel
		pi.cancel()

		gle = frappe.db.sql(
			"""select name from `tabGL Entry`
			where voucher_type='Sales Invoice' and voucher_no=%s""",
			pi.name,
		)

		self.assertFalse(gle)

	def test_purchase_invoice_update_stock_gl_entry_with_perpetual_inventory(self):

		pi = make_purchase_invoice(
			update_stock=1,
			posting_date=frappe.utils.nowdate(),
			posting_time=frappe.utils.nowtime(),
			cash_bank_account="Cash - TCP1",
			company="_Test Company with perpetual inventory",
			supplier_warehouse="Work In Progress - TCP1",
			warehouse="Stores - TCP1",
			cost_center="Main - TCP1",
			expense_account="_Test Account Cost for Goods Sold - TCP1",
		)

		gl_entries = frappe.db.sql(
			"""select account, account_currency, debit, credit,
			debit_in_account_currency, credit_in_account_currency
			from `tabGL Entry` where voucher_type='Purchase Invoice' and voucher_no=%s
			order by account asc""",
			pi.name,
			as_dict=1,
		)

		self.assertTrue(gl_entries)
		stock_in_hand_account = get_inventory_account(pi.company, pi.get("items")[0].warehouse)

		expected_gl_entries = dict(
			(d[0], d) for d in [[pi.credit_to, 0.0, 250.0], [stock_in_hand_account, 250.0, 0.0]]
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_gl_entries[gle.account][0], gle.account)
			self.assertEqual(expected_gl_entries[gle.account][1], gle.debit)
			self.assertEqual(expected_gl_entries[gle.account][2], gle.credit)

	def test_purchase_invoice_for_is_paid_and_update_stock_gl_entry_with_perpetual_inventory(self):

		pi = make_purchase_invoice(
			update_stock=1,
			posting_date=frappe.utils.nowdate(),
			posting_time=frappe.utils.nowtime(),
			cash_bank_account="Cash - TCP1",
			is_paid=1,
			company="_Test Company with perpetual inventory",
			supplier_warehouse="Work In Progress - TCP1",
			warehouse="Stores - TCP1",
			cost_center="Main - TCP1",
			expense_account="_Test Account Cost for Goods Sold - TCP1",
		)

		gl_entries = frappe.db.sql(
			"""select account, account_currency, sum(debit) as debit,
				sum(credit) as credit, debit_in_account_currency, credit_in_account_currency
			from `tabGL Entry` where voucher_type='Purchase Invoice' and voucher_no=%s
			group by account, voucher_no order by account asc;""",
			pi.name,
			as_dict=1,
		)

		stock_in_hand_account = get_inventory_account(pi.company, pi.get("items")[0].warehouse)
		self.assertTrue(gl_entries)

		expected_gl_entries = dict(
			(d[0], d)
			for d in [
				[pi.credit_to, 250.0, 250.0],
				[stock_in_hand_account, 250.0, 0.0],
				["Cash - TCP1", 0.0, 250.0],
			]
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_gl_entries[gle.account][0], gle.account)
			self.assertEqual(expected_gl_entries[gle.account][1], gle.debit)
			self.assertEqual(expected_gl_entries[gle.account][2], gle.credit)

	def test_auto_batch(self):
		item_code = frappe.db.get_value("Item", {"has_batch_no": 1, "create_new_batch": 1}, "name")

		if not item_code:
			doc = frappe.get_doc(
				{
					"doctype": "Item",
					"is_stock_item": 1,
					"item_code": "test batch item",
					"item_group": "Products",
					"has_batch_no": 1,
					"create_new_batch": 1,
				}
			).insert(ignore_permissions=True)
			item_code = doc.name

		pi = make_purchase_invoice(
			update_stock=1,
			posting_date=frappe.utils.nowdate(),
			posting_time=frappe.utils.nowtime(),
			item_code=item_code,
		)

		self.assertTrue(frappe.db.get_value("Batch", {"item": item_code, "reference_name": pi.name}))

	def test_update_stock_and_purchase_return(self):
		actual_qty_0 = get_qty_after_transaction()

		pi = make_purchase_invoice(
			update_stock=1, posting_date=frappe.utils.nowdate(), posting_time=frappe.utils.nowtime()
		)

		actual_qty_1 = get_qty_after_transaction()
		self.assertEqual(actual_qty_0 + 5, actual_qty_1)

		# return entry
		pi1 = make_purchase_invoice(is_return=1, return_against=pi.name, qty=-2, rate=50, update_stock=1)

		pi.load_from_db()
		self.assertTrue(pi.status, "Debit Note Issued")
		pi1.load_from_db()
		self.assertTrue(pi1.status, "Return")

		actual_qty_2 = get_qty_after_transaction()
		self.assertEqual(actual_qty_1 - 2, actual_qty_2)

		pi1.cancel()
		self.assertEqual(actual_qty_1, get_qty_after_transaction())

		pi.reload()
		pi.cancel()
		self.assertEqual(actual_qty_0, get_qty_after_transaction())

	def test_subcontracting_via_purchase_invoice(self):
		from erpnext.buying.doctype.purchase_order.test_purchase_order import update_backflush_based_on
		from erpnext.stock.doctype.stock_entry.test_stock_entry import make_stock_entry

		update_backflush_based_on("BOM")
		make_stock_entry(
			item_code="_Test Item", target="_Test Warehouse 1 - _TC", qty=100, basic_rate=100
		)
		make_stock_entry(
			item_code="_Test Item Home Desktop 100",
			target="_Test Warehouse 1 - _TC",
			qty=100,
			basic_rate=100,
		)

		pi = make_purchase_invoice(
			item_code="_Test FG Item", qty=10, rate=500, update_stock=1, is_subcontracted=1
		)

		self.assertEqual(len(pi.get("supplied_items")), 2)

		rm_supp_cost = sum(d.amount for d in pi.get("supplied_items"))
		self.assertEqual(flt(pi.get("items")[0].rm_supp_cost, 2), flt(rm_supp_cost, 2))

	def test_rejected_serial_no(self):
		pi = make_purchase_invoice(
			item_code="_Test Serialized Item With Series",
			received_qty=2,
			qty=1,
			rejected_qty=1,
			rate=500,
			update_stock=1,
			rejected_warehouse="_Test Rejected Warehouse - _TC",
			allow_zero_valuation_rate=1,
		)

		self.assertEqual(
			frappe.db.get_value("Serial No", pi.get("items")[0].serial_no, "warehouse"),
			pi.get("items")[0].warehouse,
		)

		self.assertEqual(
			frappe.db.get_value("Serial No", pi.get("items")[0].rejected_serial_no, "warehouse"),
			pi.get("items")[0].rejected_warehouse,
		)

	def test_outstanding_amount_after_advance_jv_cancelation(self):
		from erpnext.accounts.doctype.journal_entry.test_journal_entry import (
			test_records as jv_test_records,
		)

		jv = frappe.copy_doc(jv_test_records[1])
		jv.accounts[0].is_advance = "Yes"
		jv.insert()
		jv.submit()

		pi = frappe.copy_doc(test_records[0])
		pi.append(
			"advances",
			{
				"reference_type": "Journal Entry",
				"reference_name": jv.name,
				"reference_row": jv.get("accounts")[0].name,
				"advance_amount": 400,
				"allocated_amount": 300,
				"remarks": jv.remark,
			},
		)
		pi.insert()
		pi.submit()
		pi.load_from_db()

		# check outstanding after advance allocation
		self.assertEqual(flt(pi.outstanding_amount), flt(pi.rounded_total - pi.total_advance))

		# added to avoid Document has been modified exception
		jv = frappe.get_doc("Journal Entry", jv.name)
		jv.cancel()

		pi.load_from_db()
		# check outstanding after advance cancellation
		self.assertEqual(flt(pi.outstanding_amount), flt(pi.rounded_total + pi.total_advance))

	def test_outstanding_amount_after_advance_payment_entry_cancelation(self):
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Pay",
				"party_type": "Supplier",
				"party": "_Test Supplier",
				"company": "_Test Company",
				"paid_from_account_currency": "INR",
				"paid_to_account_currency": "INR",
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
				"reference_no": "1",
				"reference_date": nowdate(),
				"received_amount": 300,
				"paid_amount": 300,
				"paid_from": "_Test Cash - _TC",
				"paid_to": "_Test Payable - _TC",
			}
		)
		pe.insert()
		pe.submit()

		pi = frappe.copy_doc(test_records[0])
		pi.is_pos = 0
		pi.append(
			"advances",
			{
				"doctype": "Purchase Invoice Advance",
				"reference_type": "Payment Entry",
				"reference_name": pe.name,
				"advance_amount": 300,
				"allocated_amount": 300,
				"remarks": pe.remarks,
			},
		)
		pi.insert()
		pi.submit()

		pi.load_from_db()

		# check outstanding after advance allocation
		self.assertEqual(flt(pi.outstanding_amount), flt(pi.rounded_total - pi.total_advance))

		# added to avoid Document has been modified exception
		pe = frappe.get_doc("Payment Entry", pe.name)
		pe.cancel()

		pi.load_from_db()
		# check outstanding after advance cancellation
		self.assertEqual(flt(pi.outstanding_amount), flt(pi.rounded_total + pi.total_advance))

	def test_purchase_invoice_with_shipping_rule(self):
		from erpnext.accounts.doctype.shipping_rule.test_shipping_rule import create_shipping_rule

		shipping_rule = create_shipping_rule(
			shipping_rule_type="Buying", shipping_rule_name="Shipping Rule - Purchase Invoice Test"
		)

		pi = frappe.copy_doc(test_records[0])

		pi.shipping_rule = shipping_rule.name
		pi.insert()
		pi.save()

		self.assertEqual(pi.net_total, 1250)

		self.assertEqual(pi.total_taxes_and_charges, 354.1)
		self.assertEqual(pi.grand_total, 1604.1)

	def test_make_pi_without_terms(self):
		pi = make_purchase_invoice(do_not_save=1)

		self.assertFalse(pi.get("payment_schedule"))

		pi.insert()

		self.assertTrue(pi.get("payment_schedule"))

	def test_duplicate_due_date_in_terms(self):
		pi = make_purchase_invoice(do_not_save=1)
		pi.append(
			"payment_schedule", dict(due_date="2017-01-01", invoice_portion=50.00, payment_amount=50)
		)
		pi.append(
			"payment_schedule", dict(due_date="2017-01-01", invoice_portion=50.00, payment_amount=50)
		)

		self.assertRaises(frappe.ValidationError, pi.insert)

	def test_debit_note(self):
		from erpnext.accounts.doctype.payment_entry.test_payment_entry import get_payment_entry
		from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import get_outstanding_amount

		pi = make_purchase_invoice(item_code="_Test Item", qty=(5 * -1), rate=500, is_return=1)
		pi.load_from_db()
		self.assertTrue(pi.status, "Return")

		outstanding_amount = get_outstanding_amount(
			pi.doctype, pi.name, "Creditors - _TC", pi.supplier, "Supplier"
		)

		self.assertEqual(pi.outstanding_amount, outstanding_amount)

		pe = get_payment_entry("Purchase Invoice", pi.name, bank_account="_Test Bank - _TC")
		pe.reference_no = "1"
		pe.reference_date = nowdate()
		pe.paid_from_account_currency = pi.currency
		pe.paid_to_account_currency = pi.currency
		pe.source_exchange_rate = 1
		pe.target_exchange_rate = 1
		pe.paid_amount = pi.grand_total * -1
		pe.insert()
		pe.submit()

		pi_doc = frappe.get_doc("Purchase Invoice", pi.name)
		self.assertEqual(pi_doc.outstanding_amount, 0)

	def test_purchase_invoice_with_cost_center(self):
		from erpnext.accounts.doctype.cost_center.test_cost_center import create_cost_center

		cost_center = "_Test Cost Center for BS Account - _TC"
		create_cost_center(cost_center_name="_Test Cost Center for BS Account", company="_Test Company")

		pi = make_purchase_invoice_against_cost_center(
			cost_center=cost_center, credit_to="Creditors - _TC"
		)
		self.assertEqual(pi.cost_center, cost_center)

		expected_values = {
			"Creditors - _TC": {"cost_center": cost_center},
			"_Test Account Cost for Goods Sold - _TC": {"cost_center": cost_center},
		}

		gl_entries = frappe.db.sql(
			"""select account, cost_center, account_currency, debit, credit,
			debit_in_account_currency, credit_in_account_currency
			from `tabGL Entry` where voucher_type='Purchase Invoice' and voucher_no=%s
			order by account asc""",
			pi.name,
			as_dict=1,
		)

		self.assertTrue(gl_entries)

		for gle in gl_entries:
			self.assertEqual(expected_values[gle.account]["cost_center"], gle.cost_center)

	def test_purchase_invoice_without_cost_center(self):
		cost_center = "_Test Cost Center - _TC"
		pi = make_purchase_invoice(credit_to="Creditors - _TC")

		expected_values = {
			"Creditors - _TC": {"cost_center": None},
			"_Test Account Cost for Goods Sold - _TC": {"cost_center": cost_center},
		}

		gl_entries = frappe.db.sql(
			"""select account, cost_center, account_currency, debit, credit,
			debit_in_account_currency, credit_in_account_currency
			from `tabGL Entry` where voucher_type='Purchase Invoice' and voucher_no=%s
			order by account asc""",
			pi.name,
			as_dict=1,
		)

		self.assertTrue(gl_entries)

		for gle in gl_entries:
			self.assertEqual(expected_values[gle.account]["cost_center"], gle.cost_center)

	def test_purchase_invoice_with_project_link(self):
		project = make_project(
			{
				"project_name": "Purchase Invoice Project",
				"project_template_name": "Test Project Template",
				"start_date": "2020-01-01",
			}
		)
		item_project = make_project(
			{
				"project_name": "Purchase Invoice Item Project",
				"project_template_name": "Test Project Template",
				"start_date": "2019-06-01",
			}
		)

		pi = make_purchase_invoice(credit_to="Creditors - _TC", do_not_save=1)
		pi.items[0].project = item_project.name
		pi.project = project.name

		pi.submit()

		expected_values = {
			"Creditors - _TC": {"project": project.name},
			"_Test Account Cost for Goods Sold - _TC": {"project": item_project.name},
		}

		gl_entries = frappe.db.sql(
			"""select account, cost_center, project, account_currency, debit, credit,
			debit_in_account_currency, credit_in_account_currency
			from `tabGL Entry` where voucher_type='Purchase Invoice' and voucher_no=%s
			order by account asc""",
			pi.name,
			as_dict=1,
		)

		self.assertTrue(gl_entries)

		for gle in gl_entries:
			self.assertEqual(expected_values[gle.account]["project"], gle.project)

	def test_deferred_expense_via_journal_entry(self):
		deferred_account = create_account(
			account_name="Deferred Expense", parent_account="Current Assets - _TC", company="_Test Company"
		)

		acc_settings = frappe.get_doc("Accounts Settings", "Accounts Settings")
		acc_settings.book_deferred_entries_via_journal_entry = 1
		acc_settings.submit_journal_entries = 1
		acc_settings.save()

		item = create_item("_Test Item for Deferred Accounting", is_purchase_item=True)
		item.enable_deferred_expense = 1
		item.deferred_expense_account = deferred_account
		item.save()

		pi = make_purchase_invoice(item=item.name, qty=1, rate=100, do_not_save=True)
		pi.set_posting_time = 1
		pi.posting_date = "2019-01-10"
		pi.items[0].enable_deferred_expense = 1
		pi.items[0].service_start_date = "2019-01-10"
		pi.items[0].service_end_date = "2019-03-15"
		pi.items[0].deferred_expense_account = deferred_account
		pi.save()
		pi.submit()

		pda1 = frappe.get_doc(
			dict(
				doctype="Process Deferred Accounting",
				posting_date=nowdate(),
				start_date="2019-01-01",
				end_date="2019-03-31",
				type="Expense",
				company="_Test Company",
			)
		)

		pda1.insert()
		pda1.submit()

		expected_gle = [
			["_Test Account Cost for Goods Sold - _TC", 0.0, 33.85, "2019-01-31"],
			[deferred_account, 33.85, 0.0, "2019-01-31"],
			["_Test Account Cost for Goods Sold - _TC", 0.0, 43.08, "2019-02-28"],
			[deferred_account, 43.08, 0.0, "2019-02-28"],
			["_Test Account Cost for Goods Sold - _TC", 0.0, 23.07, "2019-03-15"],
			[deferred_account, 23.07, 0.0, "2019-03-15"],
		]

		gl_entries = gl_entries = frappe.db.sql(
			"""select account, debit, credit, posting_date
			from `tabGL Entry`
			where voucher_type='Journal Entry' and voucher_detail_no=%s and posting_date <= %s
			order by posting_date asc, account asc""",
			(pi.items[0].name, pi.posting_date),
			as_dict=1,
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_gle[i][0], gle.account)
			self.assertEqual(expected_gle[i][1], gle.credit)
			self.assertEqual(expected_gle[i][2], gle.debit)
			self.assertEqual(getdate(expected_gle[i][3]), gle.posting_date)

		acc_settings = frappe.get_doc("Accounts Settings", "Accounts Settings")
		acc_settings.book_deferred_entries_via_journal_entry = 0
		acc_settings.submit_journal_entriessubmit_journal_entries = 0
		acc_settings.save()

	def test_gain_loss_with_advance_entry(self):
		unlink_enabled = frappe.db.get_value(
			"Accounts Settings", "Accounts Settings", "unlink_payment_on_cancel_of_invoice"
		)

		frappe.db.set_value(
			"Accounts Settings", "Accounts Settings", "unlink_payment_on_cancel_of_invoice", 1
		)

		original_account = frappe.db.get_value("Company", "_Test Company", "exchange_gain_loss_account")
		frappe.db.set_value(
			"Company", "_Test Company", "exchange_gain_loss_account", "Exchange Gain/Loss - _TC"
		)

		pay = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"company": "_Test Company",
				"payment_type": "Pay",
				"party_type": "Supplier",
				"party": "_Test Supplier USD",
				"paid_to": "_Test Payable USD - _TC",
				"paid_from": "Cash - _TC",
				"paid_amount": 70000,
				"target_exchange_rate": 70,
				"received_amount": 1000,
			}
		)
		pay.insert()
		pay.submit()

		pi = make_purchase_invoice(
			supplier="_Test Supplier USD",
			currency="USD",
			conversion_rate=75,
			rate=500,
			do_not_save=1,
			qty=1,
		)
		pi.cost_center = "_Test Cost Center - _TC"
		pi.advances = []
		pi.append(
			"advances",
			{
				"reference_type": "Payment Entry",
				"reference_name": pay.name,
				"advance_amount": 1000,
				"remarks": pay.remarks,
				"allocated_amount": 500,
				"ref_exchange_rate": 70,
			},
		)
		pi.save()
		pi.submit()

		expected_gle = [
			["_Test Account Cost for Goods Sold - _TC", 37500.0],
			["_Test Payable USD - _TC", -35000.0],
			["Exchange Gain/Loss - _TC", -2500.0],
		]

		gl_entries = frappe.db.sql(
			"""
			select account, sum(debit - credit) as balance from `tabGL Entry`
			where voucher_no=%s
			group by account
			order by account asc""",
			(pi.name),
			as_dict=1,
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_gle[i][0], gle.account)
			self.assertEqual(expected_gle[i][1], gle.balance)

		pi_2 = make_purchase_invoice(
			supplier="_Test Supplier USD",
			currency="USD",
			conversion_rate=73,
			rate=500,
			do_not_save=1,
			qty=1,
		)
		pi_2.cost_center = "_Test Cost Center - _TC"
		pi_2.advances = []
		pi_2.append(
			"advances",
			{
				"reference_type": "Payment Entry",
				"reference_name": pay.name,
				"advance_amount": 500,
				"remarks": pay.remarks,
				"allocated_amount": 500,
				"ref_exchange_rate": 70,
			},
		)
		pi_2.save()
		pi_2.submit()

		expected_gle = [
			["_Test Account Cost for Goods Sold - _TC", 36500.0],
			["_Test Payable USD - _TC", -35000.0],
			["Exchange Gain/Loss - _TC", -1500.0],
		]

		gl_entries = frappe.db.sql(
			"""
			select account, sum(debit - credit) as balance from `tabGL Entry`
			where voucher_no=%s
			group by account order by account asc""",
			(pi_2.name),
			as_dict=1,
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_gle[i][0], gle.account)
			self.assertEqual(expected_gle[i][1], gle.balance)

		expected_gle = [["_Test Payable USD - _TC", 70000.0], ["Cash - _TC", -70000.0]]

		gl_entries = frappe.db.sql(
			"""
			select account, sum(debit - credit) as balance from `tabGL Entry`
			where voucher_no=%s and is_cancelled=0
			group by account order by account asc""",
			(pay.name),
			as_dict=1,
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_gle[i][0], gle.account)
			self.assertEqual(expected_gle[i][1], gle.balance)

		pi.reload()
		pi.cancel()

		pi_2.reload()
		pi_2.cancel()

		pay.reload()
		pay.cancel()

		frappe.db.set_value(
			"Accounts Settings", "Accounts Settings", "unlink_payment_on_cancel_of_invoice", unlink_enabled
		)
		frappe.db.set_value("Company", "_Test Company", "exchange_gain_loss_account", original_account)

	def test_purchase_invoice_advance_taxes(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		# create a new supplier to test
		supplier = create_supplier(
			supplier_name="_Test TDS Advance Supplier",
			tax_withholding_category="TDS - 194 - Dividends - Individual",
		)

		# Update tax withholding category with current fiscal year and rate details
		update_tax_witholding_category("_Test Company", "TDS Payable - _TC")

		# Create Purchase Order with TDS applied
		po = create_purchase_order(
			do_not_save=1,
			supplier=supplier.name,
			rate=3000,
			item="_Test Non Stock Item",
			posting_date="2021-09-15",
		)
		po.save()
		po.submit()

		# Create Payment Entry Against the order
		payment_entry = get_payment_entry(dt="Purchase Order", dn=po.name)
		payment_entry.paid_from = "Cash - _TC"
		payment_entry.apply_tax_withholding_amount = 1
		payment_entry.tax_withholding_category = "TDS - 194 - Dividends - Individual"
		payment_entry.save()
		payment_entry.submit()

		# Check GLE for Payment Entry
		expected_gle = [
			["Cash - _TC", 0, 27000],
			["Creditors - _TC", 30000, 0],
			["TDS Payable - _TC", 0, 3000],
		]

		gl_entries = frappe.db.sql(
			"""select account, debit, credit
			from `tabGL Entry`
			where voucher_type='Payment Entry' and voucher_no=%s
			order by account asc""",
			(payment_entry.name),
			as_dict=1,
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_gle[i][0], gle.account)
			self.assertEqual(expected_gle[i][1], gle.debit)
			self.assertEqual(expected_gle[i][2], gle.credit)

		# Create Purchase Invoice against Purchase Order
		purchase_invoice = get_mapped_purchase_invoice(po.name)
		purchase_invoice.allocate_advances_automatically = 1
		purchase_invoice.items[0].item_code = "_Test Non Stock Item"
		purchase_invoice.items[0].expense_account = "_Test Account Cost for Goods Sold - _TC"
		purchase_invoice.save()
		purchase_invoice.submit()

		# Check GLE for Purchase Invoice
		# Zero net effect on final TDS Payable on invoice
		expected_gle = [["_Test Account Cost for Goods Sold - _TC", 30000], ["Creditors - _TC", -30000]]

		gl_entries = frappe.db.sql(
			"""select account, sum(debit - credit) as amount
			from `tabGL Entry`
			where voucher_type='Purchase Invoice' and voucher_no=%s
			group by account
			order by account asc""",
			(purchase_invoice.name),
			as_dict=1,
		)

		for i, gle in enumerate(gl_entries):
			self.assertEqual(expected_gle[i][0], gle.account)
			self.assertEqual(expected_gle[i][1], gle.amount)

		payment_entry.load_from_db()
		self.assertEqual(payment_entry.taxes[0].allocated_amount, 3000)

		purchase_invoice.cancel()

		payment_entry.load_from_db()
		self.assertEqual(payment_entry.taxes[0].allocated_amount, 0)

	def test_provisional_accounting_entry(self):
		create_item("_Test Non Stock Item", is_stock_item=0)

		provisional_account = create_account(
			account_name="Provision Account",
			parent_account="Current Liabilities - _TC",
			company="_Test Company",
		)

		company = frappe.get_doc("Company", "_Test Company")
		company.enable_provisional_accounting_for_non_stock_items = 1
		company.default_provisional_account = provisional_account
		company.save()

		pr = make_purchase_receipt(
			item_code="_Test Non Stock Item", posting_date=add_days(nowdate(), -2)
		)

		pi = create_purchase_invoice_from_receipt(pr.name)
		pi.set_posting_time = 1
		pi.posting_date = add_days(pr.posting_date, -1)
		pi.items[0].expense_account = "Cost of Goods Sold - _TC"
		pi.save()
		pi.submit()

		self.assertEquals(pr.items[0].provisional_expense_account, "Provision Account - _TC")

		# Check GLE for Purchase Invoice
		expected_gle = [
			["Cost of Goods Sold - _TC", 250, 0, add_days(pr.posting_date, -1)],
			["Creditors - _TC", 0, 250, add_days(pr.posting_date, -1)],
		]

		check_gl_entries(self, pi.name, expected_gle, pi.posting_date)

		expected_gle_for_purchase_receipt = [
			["Provision Account - _TC", 250, 0, pr.posting_date],
			["_Test Account Cost for Goods Sold - _TC", 0, 250, pr.posting_date],
			["Provision Account - _TC", 0, 250, pi.posting_date],
			["_Test Account Cost for Goods Sold - _TC", 250, 0, pi.posting_date],
		]

		check_gl_entries(self, pr.name, expected_gle_for_purchase_receipt, pr.posting_date)

		# Cancel purchase invoice to check reverse provisional entry cancellation
		pi.cancel()

		expected_gle_for_purchase_receipt_post_pi_cancel = [
			["Provision Account - _TC", 0, 250, pi.posting_date],
			["_Test Account Cost for Goods Sold - _TC", 250, 0, pi.posting_date],
		]

		check_gl_entries(
			self, pr.name, expected_gle_for_purchase_receipt_post_pi_cancel, pr.posting_date
		)

		company.enable_provisional_accounting_for_non_stock_items = 0
		company.save()


def check_gl_entries(doc, voucher_no, expected_gle, posting_date):
	gl_entries = frappe.db.sql(
		"""select account, debit, credit, posting_date
		from `tabGL Entry`
		where voucher_type='Purchase Invoice' and voucher_no=%s and posting_date >= %s
		order by posting_date asc, account asc""",
		(voucher_no, posting_date),
		as_dict=1,
	)

	for i, gle in enumerate(gl_entries):
		doc.assertEqual(expected_gle[i][0], gle.account)
		doc.assertEqual(expected_gle[i][1], gle.debit)
		doc.assertEqual(expected_gle[i][2], gle.credit)
		doc.assertEqual(getdate(expected_gle[i][3]), gle.posting_date)


def update_tax_witholding_category(company, account):
	from erpnext.accounts.utils import get_fiscal_year

	fiscal_year = get_fiscal_year(date=nowdate())

	if not frappe.db.get_value(
		"Tax Withholding Rate",
		{
			"parent": "TDS - 194 - Dividends - Individual",
			"from_date": (">=", fiscal_year[1]),
			"to_date": ("<=", fiscal_year[2]),
		},
	):
		tds_category = frappe.get_doc("Tax Withholding Category", "TDS - 194 - Dividends - Individual")
		tds_category.set("rates", [])

		tds_category.append(
			"rates",
			{
				"from_date": fiscal_year[1],
				"to_date": fiscal_year[2],
				"tax_withholding_rate": 10,
				"single_threshold": 2500,
				"cumulative_threshold": 0,
			},
		)
		tds_category.save()

	if not frappe.db.get_value(
		"Tax Withholding Account", {"parent": "TDS - 194 - Dividends - Individual", "account": account}
	):
		tds_category = frappe.get_doc("Tax Withholding Category", "TDS - 194 - Dividends - Individual")
		tds_category.append("accounts", {"company": company, "account": account})
		tds_category.save()


def unlink_payment_on_cancel_of_invoice(enable=1):
	accounts_settings = frappe.get_doc("Accounts Settings")
	accounts_settings.unlink_payment_on_cancellation_of_invoice = enable
	accounts_settings.save()


def make_purchase_invoice(**args):
	pi = frappe.new_doc("Purchase Invoice")
	args = frappe._dict(args)
	pi.posting_date = args.posting_date or today()
	if args.posting_time:
		pi.posting_time = args.posting_time
	if args.update_stock:
		pi.update_stock = 1
	if args.is_paid:
		pi.is_paid = 1

	if args.cash_bank_account:
		pi.cash_bank_account = args.cash_bank_account

	pi.company = args.company or "_Test Company"
	pi.supplier = args.supplier or "_Test Supplier"
	pi.currency = args.currency or "INR"
	pi.conversion_rate = args.conversion_rate or 1
	pi.is_return = args.is_return
	pi.return_against = args.return_against
	pi.is_subcontracted = args.is_subcontracted or 0
	pi.supplier_warehouse = args.supplier_warehouse or "_Test Warehouse 1 - _TC"
	pi.cost_center = args.parent_cost_center

	pi.append(
		"items",
		{
			"item_code": args.item or args.item_code or "_Test Item",
			"warehouse": args.warehouse or "_Test Warehouse - _TC",
			"qty": args.qty or 5,
			"received_qty": args.received_qty or 0,
			"rejected_qty": args.rejected_qty or 0,
			"rate": args.rate or 50,
			"price_list_rate": args.price_list_rate or 50,
			"expense_account": args.expense_account or "_Test Account Cost for Goods Sold - _TC",
			"discount_account": args.discount_account or None,
			"discount_amount": args.discount_amount or 0,
			"conversion_factor": 1.0,
			"serial_no": args.serial_no,
			"stock_uom": args.uom or "_Test UOM",
			"cost_center": args.cost_center or "_Test Cost Center - _TC",
			"project": args.project,
			"rejected_warehouse": args.rejected_warehouse or "",
			"rejected_serial_no": args.rejected_serial_no or "",
			"asset_location": args.location or "",
			"allow_zero_valuation_rate": args.get("allow_zero_valuation_rate") or 0,
		},
	)

	if args.get_taxes_and_charges:
		taxes = get_taxes()
		for tax in taxes:
			pi.append("taxes", tax)

	if not args.do_not_save:
		pi.insert()
		if not args.do_not_submit:
			pi.submit()
	return pi


def make_purchase_invoice_against_cost_center(**args):
	pi = frappe.new_doc("Purchase Invoice")
	args = frappe._dict(args)
	pi.posting_date = args.posting_date or today()
	if args.posting_time:
		pi.posting_time = args.posting_time
	if args.update_stock:
		pi.update_stock = 1
	if args.is_paid:
		pi.is_paid = 1

	if args.cash_bank_account:
		pi.cash_bank_account = args.cash_bank_account

	pi.company = args.company or "_Test Company"
	pi.cost_center = args.cost_center or "_Test Cost Center - _TC"
	pi.supplier = args.supplier or "_Test Supplier"
	pi.currency = args.currency or "INR"
	pi.conversion_rate = args.conversion_rate or 1
	pi.is_return = args.is_return
	pi.is_return = args.is_return
	pi.credit_to = args.return_against or "Creditors - _TC"
	pi.is_subcontracted = args.is_subcontracted or 0
	if args.supplier_warehouse:
		pi.supplier_warehouse = "_Test Warehouse 1 - _TC"

	pi.append(
		"items",
		{
			"item_code": args.item or args.item_code or "_Test Item",
			"warehouse": args.warehouse or "_Test Warehouse - _TC",
			"qty": args.qty or 5,
			"received_qty": args.received_qty or 0,
			"rejected_qty": args.rejected_qty or 0,
			"rate": args.rate or 50,
			"conversion_factor": 1.0,
			"serial_no": args.serial_no,
			"stock_uom": "_Test UOM",
			"cost_center": args.cost_center or "_Test Cost Center - _TC",
			"project": args.project,
			"rejected_warehouse": args.rejected_warehouse or "",
			"rejected_serial_no": args.rejected_serial_no or "",
		},
	)
	if not args.do_not_save:
		pi.insert()
		if not args.do_not_submit:
			pi.submit()
	return pi


test_records = frappe.get_test_records("Purchase Invoice")
