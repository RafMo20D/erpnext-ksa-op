# Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, flt, now_datetime, nowdate

from erpnext.controllers.item_variant import create_variant
from erpnext.manufacturing.doctype.production_plan.production_plan import (
	get_items_for_material_requests,
	get_sales_orders,
	get_warehouse_list,
)
from erpnext.manufacturing.doctype.work_order.work_order import OverProductionError
from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order
from erpnext.stock.doctype.item.test_item import create_item
from erpnext.stock.doctype.stock_entry.test_stock_entry import make_stock_entry
from erpnext.stock.doctype.stock_reconciliation.test_stock_reconciliation import (
	create_stock_reconciliation,
)


class TestProductionPlan(FrappeTestCase):
	def setUp(self):
		for item in [
			"Test Production Item 1",
			"Subassembly Item 1",
			"Raw Material Item 1",
			"Raw Material Item 2",
		]:
			create_item(item, valuation_rate=100)

			sr = frappe.db.get_value(
				"Stock Reconciliation Item", {"item_code": item, "docstatus": 1}, "parent"
			)
			if sr:
				sr_doc = frappe.get_doc("Stock Reconciliation", sr)
				sr_doc.cancel()

		create_item("Test Non Stock Raw Material", is_stock_item=0)
		for item, raw_materials in {
			"Subassembly Item 1": ["Raw Material Item 1", "Raw Material Item 2"],
			"Test Production Item 1": [
				"Raw Material Item 1",
				"Subassembly Item 1",
				"Test Non Stock Raw Material",
			],
		}.items():
			if not frappe.db.get_value("BOM", {"item": item}):
				make_bom(item=item, raw_materials=raw_materials)

	def tearDown(self) -> None:
		frappe.db.rollback()

	def test_production_plan_mr_creation(self):
		"Test if MRs are created for unavailable raw materials."
		pln = create_production_plan(item_code="Test Production Item 1")
		self.assertTrue(len(pln.mr_items), 2)

		pln.make_material_request()
		pln.reload()
		self.assertTrue(pln.status, "Material Requested")

		material_requests = frappe.get_all(
			"Material Request Item",
			fields=["distinct parent"],
			filters={"production_plan": pln.name},
			as_list=1,
		)

		self.assertTrue(len(material_requests), 2)

		pln.make_work_order()
		work_orders = frappe.get_all(
			"Work Order", fields=["name"], filters={"production_plan": pln.name}, as_list=1
		)

		self.assertTrue(len(work_orders), len(pln.po_items))

		for name in material_requests:
			mr = frappe.get_doc("Material Request", name[0])
			if mr.docstatus != 0:
				mr.cancel()

		for name in work_orders:
			mr = frappe.delete_doc("Work Order", name[0])

		pln = frappe.get_doc("Production Plan", pln.name)
		pln.cancel()

	def test_production_plan_start_date(self):
		"Test if Work Order has same Planned Start Date as Prod Plan."
		planned_date = add_to_date(date=None, days=3)
		plan = create_production_plan(
			item_code="Test Production Item 1", planned_start_date=planned_date
		)
		plan.make_work_order()

		work_orders = frappe.get_all(
			"Work Order", fields=["name", "planned_start_date"], filters={"production_plan": plan.name}
		)

		self.assertEqual(work_orders[0].planned_start_date, planned_date)

		for wo in work_orders:
			frappe.delete_doc("Work Order", wo.name)

		plan.reload()
		plan.cancel()

	def test_production_plan_for_existing_ordered_qty(self):
		"""
		- Enable 'ignore_existing_ordered_qty'.
		- Test if MR Planning table pulls Raw Material Qty even if it is in stock.
		"""
		sr1 = create_stock_reconciliation(
			item_code="Raw Material Item 1", target="_Test Warehouse - _TC", qty=1, rate=110
		)
		sr2 = create_stock_reconciliation(
			item_code="Raw Material Item 2", target="_Test Warehouse - _TC", qty=1, rate=120
		)

		pln = create_production_plan(item_code="Test Production Item 1", ignore_existing_ordered_qty=1)
		self.assertTrue(len(pln.mr_items))
		self.assertTrue(flt(pln.mr_items[0].quantity), 1.0)

		sr1.cancel()
		sr2.cancel()
		pln.cancel()

	def test_production_plan_with_non_stock_item(self):
		"Test if MR Planning table includes Non Stock RM."
		pln = create_production_plan(item_code="Test Production Item 1", include_non_stock_items=1)
		self.assertTrue(len(pln.mr_items), 3)
		pln.cancel()

	def test_production_plan_without_multi_level(self):
		"Test MR Planning table for non exploded BOM."
		pln = create_production_plan(item_code="Test Production Item 1", use_multi_level_bom=0)
		self.assertTrue(len(pln.mr_items), 2)
		pln.cancel()

	def test_production_plan_without_multi_level_for_existing_ordered_qty(self):
		"""
		- Disable 'ignore_existing_ordered_qty'.
		- Test if MR Planning table avoids pulling Raw Material Qty as it is in stock for
		non exploded BOM.
		"""
		sr1 = create_stock_reconciliation(
			item_code="Raw Material Item 1", target="_Test Warehouse - _TC", qty=1, rate=130
		)
		sr2 = create_stock_reconciliation(
			item_code="Subassembly Item 1", target="_Test Warehouse - _TC", qty=1, rate=140
		)

		pln = create_production_plan(
			item_code="Test Production Item 1", use_multi_level_bom=0, ignore_existing_ordered_qty=0
		)
		self.assertFalse(len(pln.mr_items))

		sr1.cancel()
		sr2.cancel()
		pln.cancel()

	def test_production_plan_sales_orders(self):
		"Test if previously fulfilled SO (with WO) is pulled into Prod Plan."
		item = "Test Production Item 1"
		so = make_sales_order(item_code=item, qty=1)
		sales_order = so.name
		sales_order_item = so.items[0].name

		pln = frappe.new_doc("Production Plan")
		pln.company = so.company
		pln.get_items_from = "Sales Order"

		pln.append(
			"sales_orders",
			{
				"sales_order": so.name,
				"sales_order_date": so.transaction_date,
				"customer": so.customer,
				"grand_total": so.grand_total,
			},
		)

		pln.get_so_items()
		pln.submit()
		pln.make_work_order()

		work_order = frappe.db.get_value(
			"Work Order",
			{"sales_order": sales_order, "production_plan": pln.name, "sales_order_item": sales_order_item},
			"name",
		)

		wo_doc = frappe.get_doc("Work Order", work_order)
		wo_doc.update(
			{"wip_warehouse": "Work In Progress - _TC", "fg_warehouse": "Finished Goods - _TC"}
		)
		wo_doc.submit()

		so_wo_qty = frappe.db.get_value("Sales Order Item", sales_order_item, "work_order_qty")
		self.assertTrue(so_wo_qty, 5)

		pln = frappe.new_doc("Production Plan")
		pln.update(
			{
				"from_date": so.transaction_date,
				"to_date": so.transaction_date,
				"customer": so.customer,
				"item_code": item,
				"sales_order_status": so.status,
			}
		)
		sales_orders = get_sales_orders(pln) or {}
		sales_orders = [d.get("name") for d in sales_orders if d.get("name") == sales_order]

		self.assertEqual(sales_orders, [])

	def test_production_plan_combine_items(self):
		"Test combining FG items in Production Plan."
		item = "Test Production Item 1"
		so1 = make_sales_order(item_code=item, qty=1)

		pln = frappe.new_doc("Production Plan")
		pln.company = so1.company
		pln.get_items_from = "Sales Order"
		pln.append(
			"sales_orders",
			{
				"sales_order": so1.name,
				"sales_order_date": so1.transaction_date,
				"customer": so1.customer,
				"grand_total": so1.grand_total,
			},
		)
		so2 = make_sales_order(item_code=item, qty=2)
		pln.append(
			"sales_orders",
			{
				"sales_order": so2.name,
				"sales_order_date": so2.transaction_date,
				"customer": so2.customer,
				"grand_total": so2.grand_total,
			},
		)
		pln.combine_items = 1
		pln.get_items()
		pln.submit()

		self.assertTrue(pln.po_items[0].planned_qty, 3)

		pln.make_work_order()
		work_order = frappe.db.get_value(
			"Work Order",
			{"production_plan_item": pln.po_items[0].name, "production_plan": pln.name},
			"name",
		)

		wo_doc = frappe.get_doc("Work Order", work_order)
		wo_doc.update(
			{
				"wip_warehouse": "Work In Progress - _TC",
			}
		)

		wo_doc.submit()
		so_items = []
		for plan_reference in pln.prod_plan_references:
			so_items.append(plan_reference.sales_order_item)
			so_wo_qty = frappe.db.get_value(
				"Sales Order Item", plan_reference.sales_order_item, "work_order_qty"
			)
			self.assertEqual(so_wo_qty, plan_reference.qty)

		wo_doc.cancel()
		for so_item in so_items:
			so_wo_qty = frappe.db.get_value("Sales Order Item", so_item, "work_order_qty")
			self.assertEqual(so_wo_qty, 0.0)

		pln.reload()
		pln.cancel()

	def test_production_plan_combine_subassembly(self):
		"""
		Test combining Sub assembly items belonging to the same BOM in Prod Plan.
		1) Red-Car -> Wheel (sub assembly) > BOM-WHEEL-001
		2) Green-Car -> Wheel (sub assembly) > BOM-WHEEL-001
		"""
		from erpnext.manufacturing.doctype.bom.test_bom import create_nested_bom

		bom_tree_1 = {"Red-Car": {"Wheel": {"Rubber": {}}}}
		bom_tree_2 = {"Green-Car": {"Wheel": {"Rubber": {}}}}

		parent_bom_1 = create_nested_bom(bom_tree_1, prefix="")
		parent_bom_2 = create_nested_bom(bom_tree_2, prefix="")

		# make sure both boms use same subassembly bom
		subassembly_bom = parent_bom_1.items[0].bom_no
		frappe.db.set_value("BOM Item", parent_bom_2.items[0].name, "bom_no", subassembly_bom)

		plan = create_production_plan(item_code="Red-Car", use_multi_level_bom=1, do_not_save=True)
		plan.append(
			"po_items",
			{  # Add Green-Car to Prod Plan
				"use_multi_level_bom": 1,
				"item_code": "Green-Car",
				"bom_no": frappe.db.get_value("Item", "Green-Car", "default_bom"),
				"planned_qty": 1,
				"planned_start_date": now_datetime(),
			},
		)
		plan.get_sub_assembly_items()
		self.assertTrue(len(plan.sub_assembly_items), 2)

		plan.combine_sub_items = 1
		plan.get_sub_assembly_items()

		self.assertTrue(len(plan.sub_assembly_items), 1)  # check if sub-assembly items merged
		self.assertEqual(plan.sub_assembly_items[0].qty, 2.0)
		self.assertEqual(plan.sub_assembly_items[0].stock_qty, 2.0)

		# change warehouse in one row, sub-assemblies should not merge
		plan.po_items[0].warehouse = "Finished Goods - _TC"
		plan.get_sub_assembly_items()
		self.assertTrue(len(plan.sub_assembly_items), 2)

	def test_pp_to_mr_customer_provided(self):
		"Test Material Request from Production Plan for Customer Provided Item."
		create_item(
			"CUST-0987", is_customer_provided_item=1, customer="_Test Customer", is_purchase_item=0
		)
		create_item("Production Item CUST")

		for item, raw_materials in {
			"Production Item CUST": ["Raw Material Item 1", "CUST-0987"]
		}.items():
			if not frappe.db.get_value("BOM", {"item": item}):
				make_bom(item=item, raw_materials=raw_materials)
		production_plan = create_production_plan(item_code="Production Item CUST")
		production_plan.make_material_request()

		material_request = frappe.db.get_value(
			"Material Request Item",
			{"production_plan": production_plan.name, "item_code": "CUST-0987"},
			"parent",
		)
		mr = frappe.get_doc("Material Request", material_request)

		self.assertTrue(mr.material_request_type, "Customer Provided")
		self.assertTrue(mr.customer, "_Test Customer")

	def test_production_plan_with_multi_level_bom(self):
		"""
		Item Code	|	Qty	|
		|Test BOM 1	|	1	|
		|Test BOM 2	|	2	|
		|Test BOM 3	|	3	|
		"""

		for item_code in ["Test BOM 1", "Test BOM 2", "Test BOM 3", "Test RM BOM 1"]:
			create_item(item_code, is_stock_item=1)

		# created bom upto 3 level
		if not frappe.db.get_value("BOM", {"item": "Test BOM 3"}):
			make_bom(item="Test BOM 3", raw_materials=["Test RM BOM 1"], rm_qty=3)

		if not frappe.db.get_value("BOM", {"item": "Test BOM 2"}):
			make_bom(item="Test BOM 2", raw_materials=["Test BOM 3"], rm_qty=3)

		if not frappe.db.get_value("BOM", {"item": "Test BOM 1"}):
			make_bom(item="Test BOM 1", raw_materials=["Test BOM 2"], rm_qty=2)

		item_code = "Test BOM 1"
		pln = frappe.new_doc("Production Plan")
		pln.company = "_Test Company"
		pln.append(
			"po_items",
			{
				"item_code": item_code,
				"bom_no": frappe.db.get_value("BOM", {"item": "Test BOM 1"}),
				"planned_qty": 3,
			},
		)

		pln.get_sub_assembly_items("In House")
		pln.submit()
		pln.make_work_order()

		# last level sub-assembly work order produce qty
		to_produce_qty = frappe.db.get_value(
			"Work Order", {"production_plan": pln.name, "production_item": "Test BOM 3"}, "qty"
		)

		self.assertEqual(to_produce_qty, 18.0)
		pln.cancel()
		frappe.delete_doc("Production Plan", pln.name)

	def test_get_warehouse_list_group(self):
		"Check if required child warehouses are returned."
		warehouse_json = '[{"warehouse":"_Test Warehouse Group - _TC"}]'

		warehouses = set(get_warehouse_list(warehouse_json))
		expected_warehouses = {"_Test Warehouse Group-C1 - _TC", "_Test Warehouse Group-C2 - _TC"}

		missing_warehouse = expected_warehouses - warehouses

		self.assertTrue(
			len(missing_warehouse) == 0,
			msg=f"Following warehouses were expected {', '.join(missing_warehouse)}",
		)

	def test_get_warehouse_list_single(self):
		"Check if same warehouse is returned in absence of child warehouses."
		warehouse_json = '[{"warehouse":"_Test Scrap Warehouse - _TC"}]'

		warehouses = set(get_warehouse_list(warehouse_json))
		expected_warehouses = {
			"_Test Scrap Warehouse - _TC",
		}

		self.assertEqual(warehouses, expected_warehouses)

	def test_get_sales_order_with_variant(self):
		"Check if Template BOM is fetched in absence of Variant BOM."
		rm_item = create_item("PIV_RM", valuation_rate=100)
		if not frappe.db.exists("Item", {"item_code": "PIV"}):
			item = create_item("PIV", valuation_rate=100)
			variant_settings = {
				"attributes": [
					{"attribute": "Colour"},
				],
				"has_variants": 1,
			}
			item.update(variant_settings)
			item.save()
			parent_bom = make_bom(item="PIV", raw_materials=[rm_item.item_code])
		if not frappe.db.exists("BOM", {"item": "PIV"}):
			parent_bom = make_bom(item="PIV", raw_materials=[rm_item.item_code])
		else:
			parent_bom = frappe.get_doc("BOM", {"item": "PIV"})

		if not frappe.db.exists("Item", {"item_code": "PIV-RED"}):
			variant = create_variant("PIV", {"Colour": "Red"})
			variant.save()
			variant_bom = make_bom(item=variant.item_code, raw_materials=[rm_item.item_code])
		else:
			variant = frappe.get_doc("Item", "PIV-RED")
		if not frappe.db.exists("BOM", {"item": "PIV-RED"}):
			variant_bom = make_bom(item=variant.item_code, raw_materials=[rm_item.item_code])

		"""Testing when item variant has a BOM"""
		so = make_sales_order(item_code="PIV-RED", qty=5)
		pln = frappe.new_doc("Production Plan")
		pln.company = so.company
		pln.get_items_from = "Sales Order"
		pln.item_code = "PIV-RED"
		pln.get_open_sales_orders()
		self.assertEqual(pln.sales_orders[0].sales_order, so.name)
		pln.get_so_items()
		self.assertEqual(pln.po_items[0].item_code, "PIV-RED")
		self.assertEqual(pln.po_items[0].bom_no, variant_bom.name)
		so.cancel()
		frappe.delete_doc("Sales Order", so.name)
		variant_bom.cancel()
		frappe.delete_doc("BOM", variant_bom.name)

		"""Testing when item variant doesn't have a BOM"""
		so = make_sales_order(item_code="PIV-RED", qty=5)
		pln.get_open_sales_orders()
		self.assertEqual(pln.sales_orders[0].sales_order, so.name)
		pln.po_items = []
		pln.get_so_items()
		self.assertEqual(pln.po_items[0].item_code, "PIV-RED")
		self.assertEqual(pln.po_items[0].bom_no, parent_bom.name)

		frappe.db.rollback()

	def test_subassmebly_sorting(self):
		"Test subassembly sorting in case of multiple items with nested BOMs."
		from erpnext.manufacturing.doctype.bom.test_bom import create_nested_bom

		prefix = "_TestLevel_"
		boms = {
			"Assembly": {
				"SubAssembly1": {
					"ChildPart1": {},
					"ChildPart2": {},
				},
				"ChildPart6": {},
				"SubAssembly4": {"SubSubAssy2": {"ChildPart7": {}}},
			},
			"MegaDeepAssy": {
				"SecretSubassy": {
					"SecretPart": {"VerySecret": {"SuperSecret": {"Classified": {}}}},
				},
				# ^ assert that this is
				# first item in subassy table
			},
		}
		create_nested_bom(boms, prefix=prefix)

		items = [prefix + item_code for item_code in boms.keys()]
		plan = create_production_plan(item_code=items[0], do_not_save=True)
		plan.append(
			"po_items",
			{
				"use_multi_level_bom": 1,
				"item_code": items[1],
				"bom_no": frappe.db.get_value("Item", items[1], "default_bom"),
				"planned_qty": 1,
				"planned_start_date": now_datetime(),
			},
		)
		plan.get_sub_assembly_items()

		bom_level_order = [d.bom_level for d in plan.sub_assembly_items]
		self.assertEqual(bom_level_order, sorted(bom_level_order, reverse=True))
		# lowest most level of subassembly should be first
		self.assertIn("SuperSecret", plan.sub_assembly_items[0].production_item)

	def test_multiple_work_order_for_production_plan_item(self):
		"Test producing Prod Plan (making WO) in parts."

		def create_work_order(item, pln, qty):
			# Get Production Items
			items_data = pln.get_production_items()

			# Update qty
			items_data[(item, None, None)]["qty"] = qty

			# Create and Submit Work Order for each item in items_data
			for key, item in items_data.items():
				if pln.sub_assembly_items:
					item["use_multi_level_bom"] = 0

				wo_name = pln.create_work_order(item)
				wo_doc = frappe.get_doc("Work Order", wo_name)
				wo_doc.update(
					{"wip_warehouse": "Work In Progress - _TC", "fg_warehouse": "Finished Goods - _TC"}
				)
				wo_doc.submit()
				wo_list.append(wo_name)

		item = "Test Production Item 1"
		raw_materials = ["Raw Material Item 1", "Raw Material Item 2"]

		# Create BOM
		bom = make_bom(item=item, raw_materials=raw_materials)

		# Create Production Plan
		pln = create_production_plan(item_code=bom.item, planned_qty=5)

		# All the created Work Orders
		wo_list = []

		# Create and Submit 1st Work Order for 3 qty
		create_work_order(item, pln, 3)
		pln.reload()
		self.assertEqual(pln.po_items[0].ordered_qty, 3)

		# Create and Submit 2nd Work Order for 2 qty
		create_work_order(item, pln, 2)
		pln.reload()
		self.assertEqual(pln.po_items[0].ordered_qty, 5)

		# Overproduction
		self.assertRaises(OverProductionError, create_work_order, item=item, pln=pln, qty=2)

		# Cancel 1st Work Order
		wo1 = frappe.get_doc("Work Order", wo_list[0])
		wo1.cancel()
		pln.reload()
		self.assertEqual(pln.po_items[0].ordered_qty, 2)

		# Cancel 2nd Work Order
		wo2 = frappe.get_doc("Work Order", wo_list[1])
		wo2.cancel()
		pln.reload()
		self.assertEqual(pln.po_items[0].ordered_qty, 0)

	def test_production_plan_pending_qty_with_sales_order(self):
		"""
		Test Prod Plan impact via: SO -> Prod Plan -> WO -> SE -> SE (cancel)
		"""
		from erpnext.manufacturing.doctype.work_order.test_work_order import make_wo_order_test_record
		from erpnext.manufacturing.doctype.work_order.work_order import (
			make_stock_entry as make_se_from_wo,
		)

		make_stock_entry(
			item_code="Raw Material Item 1", target="Work In Progress - _TC", qty=2, basic_rate=100
		)
		make_stock_entry(
			item_code="Raw Material Item 2", target="Work In Progress - _TC", qty=2, basic_rate=100
		)

		item = "Test Production Item 1"
		so = make_sales_order(item_code=item, qty=1)

		pln = create_production_plan(
			company=so.company, get_items_from="Sales Order", sales_order=so, skip_getting_mr_items=True
		)
		self.assertEqual(pln.po_items[0].pending_qty, 1)

		wo = make_wo_order_test_record(
			item_code=item,
			qty=1,
			company=so.company,
			wip_warehouse="Work In Progress - _TC",
			fg_warehouse="Finished Goods - _TC",
			skip_transfer=1,
			use_multi_level_bom=1,
			do_not_submit=True,
		)
		wo.production_plan = pln.name
		wo.production_plan_item = pln.po_items[0].name
		wo.submit()

		se = frappe.get_doc(make_se_from_wo(wo.name, "Manufacture", 1))
		se.submit()

		pln.reload()
		self.assertEqual(pln.po_items[0].pending_qty, 0)

		se.cancel()
		pln.reload()
		self.assertEqual(pln.po_items[0].pending_qty, 1)

	def test_production_plan_pending_qty_independent_items(self):
		"Test Prod Plan impact if items are added independently (no from SO or MR)."
		from erpnext.manufacturing.doctype.work_order.test_work_order import make_wo_order_test_record
		from erpnext.manufacturing.doctype.work_order.work_order import (
			make_stock_entry as make_se_from_wo,
		)

		make_stock_entry(
			item_code="Raw Material Item 1", target="Work In Progress - _TC", qty=2, basic_rate=100
		)
		make_stock_entry(
			item_code="Raw Material Item 2", target="Work In Progress - _TC", qty=2, basic_rate=100
		)

		pln = create_production_plan(item_code="Test Production Item 1", skip_getting_mr_items=True)
		self.assertEqual(pln.po_items[0].pending_qty, 1)

		wo = make_wo_order_test_record(
			item_code="Test Production Item 1",
			qty=1,
			company=pln.company,
			wip_warehouse="Work In Progress - _TC",
			fg_warehouse="Finished Goods - _TC",
			skip_transfer=1,
			use_multi_level_bom=1,
			do_not_submit=True,
		)
		wo.production_plan = pln.name
		wo.production_plan_item = pln.po_items[0].name
		wo.submit()

		se = frappe.get_doc(make_se_from_wo(wo.name, "Manufacture", 1))
		se.submit()

		pln.reload()
		self.assertEqual(pln.po_items[0].pending_qty, 0)

		se.cancel()
		pln.reload()
		self.assertEqual(pln.po_items[0].pending_qty, 1)

	def test_qty_based_status(self):
		pp = frappe.new_doc("Production Plan")
		pp.po_items = [frappe._dict(planned_qty=5, produce_qty=4)]
		self.assertFalse(pp.all_items_completed())

		pp.po_items = [
			frappe._dict(planned_qty=5, produce_qty=10),
			frappe._dict(planned_qty=5, produce_qty=4),
		]
		self.assertFalse(pp.all_items_completed())

	def test_production_plan_planned_qty(self):
		pln = create_production_plan(item_code="_Test FG Item", planned_qty=0.55)
		pln.make_work_order()
		work_order = frappe.db.get_value("Work Order", {"production_plan": pln.name}, "name")
		wo_doc = frappe.get_doc("Work Order", work_order)
		wo_doc.update(
			{"wip_warehouse": "Work In Progress - _TC", "fg_warehouse": "Finished Goods - _TC"}
		)
		wo_doc.submit()
		self.assertEqual(wo_doc.qty, 0.55)

	def test_temporary_name_relinking(self):

		pp = frappe.new_doc("Production Plan")

		# this can not be unittested so mocking data that would be expected
		# from client side.
		for _ in range(10):
			po_item = pp.append(
				"po_items",
				{
					"name": frappe.generate_hash(length=10),
					"temporary_name": frappe.generate_hash(length=10),
				},
			)
			pp.append("sub_assembly_items", {"production_plan_item": po_item.temporary_name})
		pp._rename_temporary_references()

		for po_item, subassy_item in zip(pp.po_items, pp.sub_assembly_items):
			self.assertEqual(po_item.name, subassy_item.production_plan_item)

		# bad links should be erased
		pp.append("sub_assembly_items", {"production_plan_item": frappe.generate_hash(length=16)})
		pp._rename_temporary_references()
		self.assertIsNone(pp.sub_assembly_items[-1].production_plan_item)
		pp.sub_assembly_items.pop()

		# reattempting on same doc shouldn't change anything
		pp._rename_temporary_references()
		for po_item, subassy_item in zip(pp.po_items, pp.sub_assembly_items):
			self.assertEqual(po_item.name, subassy_item.production_plan_item)


def create_production_plan(**args):
	"""
	sales_order (obj): Sales Order Doc Object
	get_items_from (str): Sales Order/Material Request
	skip_getting_mr_items (bool): Whether or not to plan for new MRs
	"""
	args = frappe._dict(args)

	pln = frappe.get_doc(
		{
			"doctype": "Production Plan",
			"company": args.company or "_Test Company",
			"customer": args.customer or "_Test Customer",
			"posting_date": nowdate(),
			"include_non_stock_items": args.include_non_stock_items or 0,
			"include_subcontracted_items": args.include_subcontracted_items or 0,
			"ignore_existing_ordered_qty": args.ignore_existing_ordered_qty or 0,
			"get_items_from": "Sales Order",
		}
	)

	if not args.get("sales_order"):
		pln.append(
			"po_items",
			{
				"use_multi_level_bom": args.use_multi_level_bom or 1,
				"item_code": args.item_code,
				"bom_no": frappe.db.get_value("Item", args.item_code, "default_bom"),
				"planned_qty": args.planned_qty or 1,
				"planned_start_date": args.planned_start_date or now_datetime(),
			},
		)

	if args.get("get_items_from") == "Sales Order" and args.get("sales_order"):
		so = args.get("sales_order")
		pln.append(
			"sales_orders",
			{
				"sales_order": so.name,
				"sales_order_date": so.transaction_date,
				"customer": so.customer,
				"grand_total": so.grand_total,
			},
		)
		pln.get_items()

	if not args.get("skip_getting_mr_items"):
		mr_items = get_items_for_material_requests(pln.as_dict())
		for d in mr_items:
			pln.append("mr_items", d)

	if not args.do_not_save:
		pln.insert()
		if not args.do_not_submit:
			pln.submit()

	return pln


def make_bom(**args):
	args = frappe._dict(args)

	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"is_default": 1,
			"item": args.item,
			"currency": args.currency or "USD",
			"quantity": args.quantity or 1,
			"company": args.company or "_Test Company",
			"routing": args.routing,
			"with_operations": args.with_operations or 0,
		}
	)

	for item in args.raw_materials:
		item_doc = frappe.get_doc("Item", item)

		bom.append(
			"items",
			{
				"item_code": item,
				"qty": args.rm_qty or 1.0,
				"uom": item_doc.stock_uom,
				"stock_uom": item_doc.stock_uom,
				"rate": item_doc.valuation_rate or args.rate,
				"source_warehouse": args.source_warehouse,
			},
		)

	if not args.do_not_save:
		bom.insert(ignore_permissions=True)

		if not args.do_not_submit:
			bom.submit()

	return bom
