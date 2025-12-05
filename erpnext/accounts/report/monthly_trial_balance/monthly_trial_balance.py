# Copyright (c) 2025, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate, add_months, flt
from datetime import timedelta
from frappe.query_builder.functions import Sum
from erpnext.accounts.report.financial_statements import filter_accounts, filter_out_zero_value_rows
from erpnext.accounts.report.utils import convert_to_presentation_currency, get_currency
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
    get_accounting_dimensions,
    get_dimension_with_children,
)

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
			"opening_debit": 0.0,
			"opening_credit": 0.0,
		}
		opening_net = flt(d.get("opening_debit", 0)) - flt(d.get("opening_credit", 0))
		row["opening_debit"] = opening_net if opening_net > 0 else 0.0
		row["opening_credit"] = abs(opening_net) if opening_net < 0 else 0.0
		running_net = opening_net
		period_debit_total = 0
		period_credit_total = 0
		for label, _s, _e in months:
			odr = running_net if running_net > 0 else 0
			ocr = abs(running_net) if running_net < 0 else 0
			row[f"{label}_opening_debit"] = odr
			row[f"{label}_opening_credit"] = ocr
			md = flt(d.get(f"{label}_debit", 0))
			mc = flt(d.get(f"{label}_credit", 0))
			row[f"{label}_debit"] = md
			row[f"{label}_credit"] = mc
			closing_net = running_net + (md - mc)
			cdr = closing_net if closing_net > 0 else 0
			ccr = abs(closing_net) if closing_net < 0 else 0
			row[f"{label}_closing_debit"] = cdr
			row[f"{label}_closing_credit"] = ccr
			period_debit_total += md
			period_credit_total += mc
			running_net = closing_net
		net = running_net
		row["closing_debit"] = net if net > 0 else 0
		row["closing_credit"] = abs(net) if net < 0 else 0
		row_has = (abs(row["opening_debit"]) >= 0.005) or (abs(row["opening_credit"]) >= 0.005)
		if not row_has:
			for label, _s, _e in months:
				if (abs(row.get(f"{label}_closing_debit", 0)) >= 0.005) or (abs(row.get(f"{label}_closing_credit", 0)) >= 0.005):
					row_has = True
					break
		if not row_has:
			row_has = (abs(row["closing_debit"]) >= 0.005) or (abs(row["closing_credit"]) >= 0.005)
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
		company_currency = frappe.get_cached_value("Company", filters.company, "default_currency")
		do_convert = bool(filters.get("presentation_currency")) and filters.presentation_currency != company_currency
		if do_convert:
			query = (
				frappe.qb.from_(gle)
				.select(
					gle.account,
					gle.company,
					gle.posting_date,
					gle.debit,
					gle.credit,
					gle.debit_in_account_currency,
					gle.credit_in_account_currency,
					gle.account_currency,
				)
				.where((gle.company == filters.company) & (gle.posting_date >= start_date) & (gle.posting_date <= end_date) & (gle.is_cancelled == 0))
			)
		else:
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
			company_fb = frappe.get_cached_value("Company", filters.company, "default_finance_book")
			if filters.get("include_default_book_entries"):
				query = query.where((gle.finance_book.isin([filters.finance_book, company_fb, ""])) | (gle.finance_book.isnull()))
			else:
				query = query.where((gle.finance_book.isin([filters.finance_book, ""])) | (gle.finance_book.isnull()))

		dims = get_accounting_dimensions(as_list=False)
		for dim in dims:
			dim_val = filters.get(dim.fieldname)
			if dim_val:
				is_tree = frappe.get_cached_value("DocType", dim.document_type, "is_tree")
				values = dim_val
				if is_tree:
					values = get_dimension_with_children(dim.document_type, dim_val)
				if not isinstance(values, (list, tuple)):
					values = [values]
				query = query.where(gle[dim.fieldname].isin(values))

		rows = query.run(as_dict=True)
		if do_convert:
			convert_to_presentation_currency(rows, get_currency(filters))
			by_acc = {}
			for r in rows:
				acc = r["account"]
				by_acc.setdefault(acc, {"debit": 0.0, "credit": 0.0})
				by_acc[acc]["debit"] += flt(r.get("debit", 0))
				by_acc[acc]["credit"] += flt(r.get("credit", 0))
			for acc, sums in by_acc.items():
				out.setdefault(acc, {})[label] = {"debit": sums["debit"], "credit": sums["credit"]}
		else:
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
		cols.append({"fieldname": f"{label}_opening_debit", "label": _(f"{label} Opening (Dr)"), "fieldtype": "Currency", "options": "currency", "width": 120})
		cols.append({"fieldname": f"{label}_opening_credit", "label": _(f"{label} Opening (Cr)"), "fieldtype": "Currency", "options": "currency", "width": 120})
		cols.append({"fieldname": f"{label}_debit", "label": _(f"{label} (Dr)"), "fieldtype": "Currency", "options": "currency", "width": 120})
		cols.append({"fieldname": f"{label}_credit", "label": _(f"{label} (Cr)"), "fieldtype": "Currency", "options": "currency", "width": 120})
		cols.append({"fieldname": f"{label}_closing_debit", "label": _(f"{label} Closing (Dr)"), "fieldtype": "Currency", "options": "currency", "width": 120})
		cols.append({"fieldname": f"{label}_closing_credit", "label": _(f"{label} Closing (Cr)"), "fieldtype": "Currency", "options": "currency", "width": 120})
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
