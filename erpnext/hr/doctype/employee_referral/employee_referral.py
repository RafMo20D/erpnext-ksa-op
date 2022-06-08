# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_link_to_form

from erpnext.hr.utils import validate_active_employee


class EmployeeReferral(Document):
	def validate(self):
		validate_active_employee(self.referrer)
		self.set_full_name()
		self.set_referral_bonus_payment_status()

	def set_full_name(self):
		self.full_name = " ".join(filter(None, [self.first_name, self.last_name]))

	def set_referral_bonus_payment_status(self):
		if not self.is_applicable_for_referral_bonus:
			self.referral_payment_status = ""
		else:
			if not self.referral_payment_status:
				self.referral_payment_status = "Unpaid"


@frappe.whitelist()
def create_job_applicant(source_name, target_doc=None):
	emp_ref = frappe.get_doc("Employee Referral", source_name)
	# just for Api call if some set status apart from default Status
	status = emp_ref.status
	if emp_ref.status in ["Pending", "In process"]:
		status = "Open"

	job_applicant = frappe.new_doc("Job Applicant")
	job_applicant.source = "Employee Referral"
	job_applicant.employee_referral = emp_ref.name
	job_applicant.status = status
	job_applicant.designation = emp_ref.for_designation
	job_applicant.applicant_name = emp_ref.full_name
	job_applicant.email_id = emp_ref.email
	job_applicant.phone_number = emp_ref.contact_no
	job_applicant.resume_attachment = emp_ref.resume
	job_applicant.resume_link = emp_ref.resume_link
	job_applicant.save()

	frappe.msgprint(
		_("Job Applicant {0} created successfully.").format(
			get_link_to_form("Job Applicant", job_applicant.name)
		),
		title=_("Success"),
		indicator="green",
	)

	emp_ref.db_set("status", "In Process")

	return job_applicant


@frappe.whitelist()
def create_additional_salary(doc):
	import json

	if isinstance(doc, str):
		doc = frappe._dict(json.loads(doc))

	if not frappe.db.exists("Additional Salary", {"ref_docname": doc.name}):
		additional_salary = frappe.new_doc("Additional Salary")
		additional_salary.employee = doc.referrer
		additional_salary.company = frappe.db.get_value("Employee", doc.referrer, "company")
		additional_salary.overwrite_salary_structure_amount = 0
		additional_salary.ref_doctype = doc.doctype
		additional_salary.ref_docname = doc.name

	return additional_salary
