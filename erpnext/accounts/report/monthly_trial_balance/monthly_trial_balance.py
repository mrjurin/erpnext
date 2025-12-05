# Copyright (c) 2025, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate, add_months, flt
from datetime import timedelta
from frappe.query_builder.functions import Sum
from erpnext.accounts.report.financial_statements import filter_accounts, filter_out_zero_value_rows

from erpnext.accounts.report.trial_balance.trial_balance import get_opening_balances


def execute(filters=None):
	filters = frappe._dict(filters or {})
	validate_filters(filters)

	company_currency = frappe.get_cached_value("Company", filters.company, "default_currency")
	months = get_month_ranges(filters.from_date, filters.to_date)

	opening = get_opening_balances(filters)
	monthly = get_monthly_sums(filters, months)

	accounts_raw = frappe.db.sql(
		"""
		select name, account_number, parent_account, account_name, root_type, report_type, lft, rgt, is_group
		from `tabAccount` where company=%s order by lft
		""",
		filters.company,
		as_dict=True,
	)
	if not accounts_raw:
		return build_columns(months), []

	accounts, accounts_by_name, parent_children_map = filter_accounts(accounts_raw)

	for acc in accounts_by_name:
		vals = opening.get(acc, {})
		accounts_by_name[acc]["opening_debit"] = flt(vals.get("opening_debit", 0))
		accounts_by_name[acc]["opening_credit"] = flt(vals.get("opening_credit", 0))
		for label, _s, _e in months:
			m = monthly.get(acc, {}).get(label, {})
			accounts_by_name[acc][f"{label}_debit"] = flt(m.get("debit", 0))
			accounts_by_name[acc][f"{label}_credit"] = flt(m.get("credit", 0))

	for d in reversed(accounts):
		if d.parent_account:
			pa = accounts_by_name[d.parent_account]
			pa["opening_debit"] = flt(pa.get("opening_debit", 0)) + flt(d.get("opening_debit", 0))
			pa["opening_credit"] = flt(pa.get("opening_credit", 0)) + flt(d.get("opening_credit", 0))
			for label, _s, _e in months:
				pa[f"{label}_debit"] = flt(pa.get(f"{label}_debit", 0)) + flt(d.get(f"{label}_debit", 0))
				pa[f"{label}_credit"] = flt(pa.get(f"{label}_credit", 0)) + flt(d.get(f"{label}_credit", 0))

	columns = build_columns(months)
	data = []
	for d in accounts:
		row = {
			"account": d.name,
			"parent_account": d.parent_account or "",
			"indent": flt(d.indent),
			"account_name": get_account_label(d.account_number, d.account_name),
			"currency": company_currency,
			"opening_debit": flt(d.get("opening_debit", 0)),
			"opening_credit": flt(d.get("opening_credit", 0)),
		}
		pd = 0
		pc = 0
		for label, _s, _e in months:
			row[f"{label}_debit"] = flt(d.get(f"{label}_debit", 0))
			row[f"{label}_credit"] = flt(d.get(f"{label}_credit", 0))
			pd += row[f"{label}_debit"]
			pc += row[f"{label}_credit"]
		net = (row["opening_debit"] - row["opening_credit"]) + (pd - pc)
		row["closing_debit"] = net if net > 0 else 0
		row["closing_credit"] = abs(net) if net < 0 else 0
		row_has = row["opening_debit"] or row["opening_credit"] or row["closing_debit"] or row["closing_credit"]
		if not row_has:
			for label, _s, _e in months:
				if row.get(f"{label}_debit") or row.get(f"{label}_credit"):
					row_has = True
					break
		row["has_value"] = 1 if row_has else 0
		data.append(row)

	if not filters.get("show_zero_values"):
		data = prune_zero_rows(data, parent_children_map)

	return columns, data


def validate_filters(filters):
	if not filters.get("company"):
		frappe.throw("Company is required")
	if not filters.get("from_date") or not filters.get("to_date"):
		frappe.throw("From Date and To Date are required")
	if getdate(filters.from_date) > getdate(filters.to_date):
		frappe.throw("From Date must be before To Date")


def get_month_ranges(from_date, to_date):
	start = getdate(from_date)
	end = getdate(to_date)

	month_start = start.replace(day=1)
	months = []
	while month_start <= end:
		label = month_start.strftime("%Y-%m")
		# month end: next month start - 1 day
		next_month_start = add_months(month_start, 1)
		month_end = (next_month_start - timedelta(days=1))
		if month_end > end:
			month_end = end
		months.append((label, month_start, month_end))
		month_start = next_month_start
	return months


def get_monthly_sums(filters, months):
	gle = frappe.qb.DocType("GL Entry")

	out = {}
	for label, start_date, end_date in months:
		query = (
			frappe.qb.from_(gle)
			.select(gle.account, Sum(gle.debit).as_("debit"), Sum(gle.credit).as_("credit"))
			.where((gle.company == filters.company) & (gle.posting_date >= start_date) & (gle.posting_date <= end_date) & (gle.is_cancelled == 0))
			.groupby(gle.account)
		)

		if filters.get("cost_center"):
			query = query.where(gle.cost_center == filters.cost_center)
		if filters.get("project"):
			query = query.where(gle.project == filters.project)
		if filters.get("finance_book"):
			query = query.where(gle.finance_book == filters.finance_book)

		rows = query.run(as_dict=True)
		for r in rows:
			out.setdefault(r.account, {})[label] = {"debit": r.debit or 0, "credit": r.credit or 0}

	return out


def get_account_info(accounts):
	if not accounts:
		return {}
	info = {}
	for d in frappe.get_all(
		"Account",
		fields=["name", "account_name", "account_number"],
		filters={"name": ("in", accounts)},
	):
		name = d.name
		label = f"{d.account_number} - {d.account_name}" if d.get("account_number") else d.get("account_name")
		info[name] = {"account_name": label}
	return info


def get_account_label(number, name):
	return f"{number} - {name}" if number else name


def prune_zero_rows(data, parent_children_map):
	out = []
	for d in data:
		if d.get("has_value"):
			out.append(d)
			continue
		children = [child.name for child in parent_children_map.get(d.get("account")) or []]
		if children:
			for row in data:
				if row.get("account") in children and row.get("has_value"):
					out.append(d)
					break
	return out


def build_columns(months):
	cols = [
		{"fieldname": "account", "label": _("Account"), "fieldtype": "Link", "options": "Account", "width": 300},
		{"fieldname": "currency", "label": _("Currency"), "fieldtype": "Link", "options": "Currency", "hidden": 1},
		{"fieldname": "opening_debit", "label": _("Opening (Dr)"), "fieldtype": "Currency", "options": "currency", "width": 120},
		{"fieldname": "opening_credit", "label": _("Opening (Cr)"), "fieldtype": "Currency", "options": "currency", "width": 120},
	]
	for label, _start, _end in months:
		cols.append({"fieldname": f"{label}_debit", "label": _(f"{label} (Dr)"), "fieldtype": "Currency", "options": "currency", "width": 120})
		cols.append({"fieldname": f"{label}_credit", "label": _(f"{label} (Cr)"), "fieldtype": "Currency", "options": "currency", "width": 120})
	cols.extend([
		{"fieldname": "closing_debit", "label": _("Closing (Dr)"), "fieldtype": "Currency", "options": "currency", "width": 120},
		{"fieldname": "closing_credit", "label": _("Closing (Cr)"), "fieldtype": "Currency", "options": "currency", "width": 120},
	])
	return cols


def is_all_zero(row, months):
	if any([row.get("opening_debit"), row.get("opening_credit"), row.get("closing_debit"), row.get("closing_credit")]):
		return False
	for label, _s, _e in months:
		if row.get(f"{label}_debit") or row.get(f"{label}_credit"):
			return False
	return True
