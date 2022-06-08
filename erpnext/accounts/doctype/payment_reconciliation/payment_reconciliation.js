// Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
// For license information, please see license.txt

frappe.provide("erpnext.accounts");
erpnext.accounts.PaymentReconciliationController = class PaymentReconciliationController extends frappe.ui.form.Controller {
	onload() {
		const default_company = frappe.defaults.get_default('company');
		this.frm.set_value('company', default_company);

		this.frm.set_value('party_type', '');
		this.frm.set_value('party', '');
		this.frm.set_value('receivable_payable_account', '');

		this.frm.set_query("party_type", () => {
			return {
				"filters": {
					"name": ["in", Object.keys(frappe.boot.party_account_types)],
				}
			}
		});

		this.frm.set_query('receivable_payable_account', () => {
			return {
				filters: {
					"company": this.frm.doc.company,
					"is_group": 0,
					"account_type": frappe.boot.party_account_types[this.frm.doc.party_type]
				}
			};
		});

		this.frm.set_query('bank_cash_account', () => {
			return {
				filters:[
					['Account', 'company', '=', this.frm.doc.company],
					['Account', 'is_group', '=', 0],
					['Account', 'account_type', 'in', ['Bank', 'Cash']]
				]
			};
		});

		this.frm.set_query("cost_center", () => {
			return {
				"filters": {
					"company": this.frm.doc.company,
					"is_group": 0
				}
			}
		});
	}

	refresh() {
		this.frm.disable_save();

		this.frm.set_df_property('invoices', 'cannot_delete_rows', true);
		this.frm.set_df_property('payments', 'cannot_delete_rows', true);
		this.frm.set_df_property('allocation', 'cannot_delete_rows', true);

		this.frm.set_df_property('invoices', 'cannot_add_rows', true);
		this.frm.set_df_property('payments', 'cannot_add_rows', true);
		this.frm.set_df_property('allocation', 'cannot_add_rows', true);


		if (this.frm.doc.receivable_payable_account) {
			this.frm.add_custom_button(__('Get Unreconciled Entries'), () =>
				this.frm.trigger("get_unreconciled_entries")
			);
			this.frm.change_custom_button_type('Get Unreconciled Entries', null, 'primary');
		}
		if (this.frm.doc.invoices.length && this.frm.doc.payments.length) {
			this.frm.add_custom_button(__('Allocate'), () =>
				this.frm.trigger("allocate")
			);
			this.frm.change_custom_button_type('Allocate', null, 'primary');
			this.frm.change_custom_button_type('Get Unreconciled Entries', null, 'default');
		}
		if (this.frm.doc.allocation.length) {
			this.frm.add_custom_button(__('Reconcile'), () =>
				this.frm.trigger("reconcile")
			);
			this.frm.change_custom_button_type('Reconcile', null, 'primary');
			this.frm.change_custom_button_type('Get Unreconciled Entries', null, 'default');
			this.frm.change_custom_button_type('Allocate', null, 'default');
		}
	}

	company() {
		this.frm.set_value('party', '');
		this.frm.set_value('receivable_payable_account', '');
	}

	party_type() {
		this.frm.set_value('party', '');
	}

	party() {
		this.frm.set_value('receivable_payable_account', '');
		this.frm.trigger("clear_child_tables");

		if (!this.frm.doc.receivable_payable_account && this.frm.doc.party_type && this.frm.doc.party) {
			return frappe.call({
				method: "erpnext.accounts.party.get_party_account",
				args: {
					company: this.frm.doc.company,
					party_type: this.frm.doc.party_type,
					party: this.frm.doc.party
				},
				callback: (r) => {
					if (!r.exc && r.message) {
						this.frm.set_value("receivable_payable_account", r.message);
					}
					this.frm.refresh();

				}
			});
		}
	}

	receivable_payable_account() {
		this.frm.trigger("clear_child_tables");
		this.frm.refresh();
	}

	clear_child_tables() {
		this.frm.clear_table("invoices");
		this.frm.clear_table("payments");
		this.frm.clear_table("allocation");
		this.frm.refresh_fields();
	}

	get_unreconciled_entries() {
		this.frm.clear_table("allocation");
		return this.frm.call({
			doc: this.frm.doc,
			method: 'get_unreconciled_entries',
			callback: () => {
				if (!(this.frm.doc.payments.length || this.frm.doc.invoices.length)) {
					frappe.throw({message: __("No Unreconciled Invoices and Payments found for this party and account")});
				} else if (!(this.frm.doc.invoices.length)) {
					frappe.throw({message: __("No Outstanding Invoices found for this party")});
				} else if (!(this.frm.doc.payments.length)) {
					frappe.throw({message: __("No Unreconciled Payments found for this party")});
				}
				this.frm.refresh();
			}
		});

	}

	allocate() {
		let payments = this.frm.fields_dict.payments.grid.get_selected_children();
		if (!(payments.length)) {
			payments = this.frm.doc.payments;
		}
		let invoices = this.frm.fields_dict.invoices.grid.get_selected_children();
		if (!(invoices.length)) {
			invoices = this.frm.doc.invoices;
		}
		return this.frm.call({
			doc: this.frm.doc,
			method: 'allocate_entries',
			args: {
				payments: payments,
				invoices: invoices
			},
			callback: () => {
				this.frm.refresh();
			}
		});
	}

	reconcile() {
		var show_dialog = this.frm.doc.allocation.filter(d => d.difference_amount && !d.difference_account);

		if (show_dialog && show_dialog.length) {

			this.data = [];
			const dialog = new frappe.ui.Dialog({
				title: __("Select Difference Account"),
				fields: [
					{
						fieldname: "allocation", fieldtype: "Table", label: __("Allocation"),
						data: this.data, in_place_edit: true,
						get_data: () => {
							return this.data;
						},
						fields: [{
							fieldtype:'Data',
							fieldname:"docname",
							in_list_view: 1,
							hidden: 1
						}, {
							fieldtype:'Data',
							fieldname:"reference_name",
							label: __("Voucher No"),
							in_list_view: 1,
							read_only: 1
						}, {
							fieldtype:'Link',
							options: 'Account',
							in_list_view: 1,
							label: __("Difference Account"),
							fieldname: 'difference_account',
							reqd: 1,
							get_query: () => {
								return {
									filters: {
										company: this.frm.doc.company,
										is_group: 0
									}
								}
							}
						}, {
							fieldtype:'Currency',
							in_list_view: 1,
							label: __("Difference Amount"),
							fieldname: 'difference_amount',
							read_only: 1
						}]
					},
				],
				primary_action: () => {
					const args = dialog.get_values()["allocation"];

					args.forEach(d => {
						frappe.model.set_value("Payment Reconciliation Allocation", d.docname,
							"difference_account", d.difference_account);
					});

					this.reconcile_payment_entries();
					dialog.hide();
				},
				primary_action_label: __('Reconcile Entries')
			});

			this.frm.doc.allocation.forEach(d => {
				if (d.difference_amount && !d.difference_account) {
					dialog.fields_dict.allocation.df.data.push({
						'docname': d.name,
						'reference_name': d.reference_name,
						'difference_amount': d.difference_amount,
						'difference_account': d.difference_account,
					});
				}
			});

			this.data = dialog.fields_dict.allocation.df.data;
			dialog.fields_dict.allocation.grid.refresh();
			dialog.show();
		} else {
			this.reconcile_payment_entries();
		}
	}

	reconcile_payment_entries() {
		return this.frm.call({
			doc: this.frm.doc,
			method: 'reconcile',
			callback: () => {
				this.frm.clear_table("allocation");
				this.frm.refresh();
			}
		});
	}
};

extend_cscript(cur_frm.cscript, new erpnext.accounts.PaymentReconciliationController({frm: cur_frm}));
