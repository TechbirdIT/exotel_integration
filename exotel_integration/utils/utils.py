import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def get_communication_channel_options():
	"""Return the current options of Communication Medium.communication_channel.

	Returns None if the field is not available (eg. erpnext not installed yet).
	The field ships with empty options in some erpnext versions, so guard against None.
	"""
	meta = frappe.get_meta("Communication Medium")
	field = meta.get_field("communication_channel")
	if not field:
		return None

	return [option for option in (field.options or "").split("\n") if option.strip()]


def set_communication_channel_options(options):
	make_property_setter(
		"Communication Medium",
		"communication_channel",
		"options",
		"\n".join(options),
		"Text",
		validate_fields_for_doctype=False
	)


def add_exotel_option():
	options = get_communication_channel_options()
	if options is None:
		return

	if "Exotel" not in options:
		options.append("Exotel")
		set_communication_channel_options(options)


def remove_exotel_option():
	options = get_communication_channel_options()
	if options is None:
		return

	if "Exotel" in options:
		options.remove("Exotel")
		set_communication_channel_options(options)
