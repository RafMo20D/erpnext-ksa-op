from frappe import _


def get_data():
	return {
		"fieldname": "reference_name",
		"non_standard_fieldnames": {
			"Additional Salary": "ref_docname",
		},
		"transactions": [{"label": _("Payment"), "items": ["Payment Entry", "Additional Salary"]}],
	}
