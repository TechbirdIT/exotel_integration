import json

import bleach
import frappe
import requests
from frappe import _
from frappe.integrations.utils import create_request_log
from datetime import timedelta, datetime, time

# Endpoints for webhook
#
# Incoming Call:
# <site>/api/method/exotel_integration.handler.handle_request?key=<exotel-integration-key>

# Exotel Reference:
# https://developer.exotel.com/api/
# https://support.exotel.com/support/solutions/articles/48283-working-with-passthru-applet


@frappe.whitelist(allow_guest=True)
def handle_request(**kwargs):
	validate_request()
	if not is_integration_enabled():
		return

	request_log = create_request_log(
		kwargs,
		request_description="Exotel Call",
		service_name="Exotel",
		request_headers=frappe.request.headers,
		is_remote_request=1
	)
	try:
		request_log.status = "Completed"
		exotel_settings = get_exotel_settings()
		if not exotel_settings.enabled:
			return

		call_payload = kwargs
		status = call_payload.get("Status")
		if status == "free":
			return

		if call_log := get_call_log(call_payload):
			update_call_log(call_payload, call_log=call_log)
		else:
			create_call_log(
				call_id=call_payload.get("CallSid"),
				from_number=call_payload.get("CallFrom"),
				to_number=call_payload.get("DialWhomNumber"),
				medium=call_payload.get("To"),
				status=get_call_log_status(call_payload),
			)
	except Exception as e:
		request_log.status = "Failed"
		request_log.error = frappe.get_traceback()
		frappe.db.rollback()
		frappe.db.commit()
	finally:
		request_log.save(ignore_permissions=True)
		frappe.db.commit()


def update_call_log(call_payload, status="Ringing", call_log=None):
	call_log = call_log or get_call_log(call_payload)
	status = get_call_log_status(call_payload)
	try:
		if call_log:
			call_log.status = status
			# resetting this because call might be redirected to other number
			call_log.to = call_payload.get("DialWhomNumber")
			call_log.duration = call_payload.get("DialCallDuration") or call_payload.get('ConversationDuration') or 0
			call_log.recording_url = call_payload.get("RecordingUrl")
			call_log.start_time = call_payload.get("StartTime")
			call_log.end_time = call_payload.get("EndTime")
			call_log.save(ignore_permissions=True)
			frappe.db.commit()
			return call_log
	except Exception as e:
		frappe.db.commit()


def get_call_log_status(call_payload):
	status = call_payload.get("DialCallStatus")
	call_type = call_payload.get("CallType")
	dial_call_status = call_payload.get("DialCallStatus")

	if call_type == "incomplete" and dial_call_status == "no-answer":
		status = "No Answer"
	elif call_type == "client-hangup" and dial_call_status == "canceled":
		status = "Canceled"
	elif call_type == "incomplete" and dial_call_status == "failed":
		status = "Failed"
	elif call_type == "completed":
		status = "Completed"
	elif dial_call_status == "busy":
		status = "Ringing"

	return status


def get_call_log(call_payload):
	call_log_id = call_payload.get("CallSid")
	if frappe.db.exists("Call Log", call_log_id):
		return frappe.get_doc("Call Log", call_log_id)


def create_call_log(
	call_id,
	from_number,
	to_number,
	medium,
	status="Ringing",
	call_type="Incoming",
	link_to_document=None,
):
	call_log = frappe.new_doc("Call Log")
	call_log.id = call_id
	call_log.to = to_number
	call_log.medium = medium
	call_log.type = call_type
	call_log.status = status
	setattr(call_log, "from", from_number)
	if link_to_document:
		call_log.append("links", link_to_document)
	call_log.save(ignore_permissions=True)
	frappe.db.commit()
	return call_log


@frappe.whitelist()
def get_call_status(call_id):
	endpoint = get_exotel_endpoint("Calls/{call_id}.json".format(call_id=call_id))
	response = requests.get(endpoint)
	return response.json().get("Call", {}).get("Status")


@frappe.whitelist()
def make_a_call(to_number, caller_id=None, link_to_document=None):
	if not is_integration_enabled():
		frappe.throw(
			_("Please setup Exotel intergration"), title=_("Integration Not Enabled")
		)

	endpoint = get_exotel_endpoint("Calls/connect.json?details=true")
	cell_number = frappe.get_value(
		"Employee", {"user_id": frappe.session.user}, "cell_number"
	)

	if not cell_number:
		frappe.throw(_("You do not have mobile number set in your Employee master"))

	try:
		response = requests.post(
			endpoint,
			data={
				"From": cell_number,
				"To": to_number,
				"CallerId": caller_id,
				"Record": "true"
				if frappe.db.get_single_value("Exotel Settings", "record_call")
				else "false",
				"StatusCallback": get_status_updater_url(),
				"StatusCallbackEvents[0]": "terminal",
			},
		)
		response.raise_for_status()
	except requests.exceptions.HTTPError as e:
		if exc := response.json().get("RestException"):
			frappe.throw(bleach.linkify(exc.get("Message")), title=_("Exotel Exception"))
	else:
		res = response.json()
		call_payload = res.get("Call", {})
		if link_to_document:
			link_to_document = json.loads(link_to_document)
		create_call_log(
			call_id=call_payload.get("Sid"),
			from_number=call_payload.get("From"),
			to_number=call_payload.get("To"),
			medium=call_payload.get("PhoneNumberSid"),
			call_type="Outgoing",
			link_to_document=link_to_document,
		)

	return response.json()

def get_status_updater_url():
	from frappe.utils.data import get_url

	webhook_key = frappe.db.get_single_value("Exotel Settings", "webhook_key")
	return get_url(
		f"api/method/exotel_integration.handler.handle_request?key={webhook_key}"
	)


def get_exotel_settings():
	return frappe.get_single("Exotel Settings")


def whitelist_numbers(numbers, caller_id):
	endpoint = get_exotel_endpoint("CustomerWhitelist")
	return requests.post(
		endpoint,
		data={
			"VirtualNumber": caller_id,
			"Number": numbers,
		},
	)


@frappe.whitelist()
def get_all_exophones():
	endpoint = get_exotel_endpoint("IncomingPhoneNumbers.json")
	response = requests.get(endpoint)
	return [
		phone.get("IncomingPhoneNumber", {}).get("PhoneNumber")
		for phone in response.json().get("IncomingPhoneNumbers", [])
	]


def get_exotel_endpoint(action):
	settings = get_exotel_settings()
	return (
		"https://{api_key}:{api_token}@api.exotel.com/v1/Accounts/{sid}/{action}".format(
			api_key=settings.api_key,
			api_token=settings.get_password("api_token"),
			sid=settings.account_sid,
			action=action,
		)
	)


def validate_request():
	# workaround security since exotel does not support request signature
	# /api/method/<exotel-integration-method>?key=<exotel-integration-key>
	webhook_key = frappe.db.get_single_value("Exotel Settings", "webhook_key")
	key = frappe.request.args.get('key')
	is_valid = key and key == webhook_key

	if not is_valid:
		frappe.throw(_("Unauthorized request"), exc=frappe.PermissionError)


@frappe.whitelist()
def is_integration_enabled():
	return frappe.db.get_single_value("Exotel Settings", "enabled", True)


# @frappe.whitelist()
# def get_available_agent_phone_number():
#     """
#     Query your CRM system (replace with your actual CRM logic) 
#     to get the phone number of an available agent. 
#     """

#     # Example (replace with your actual CRM query)
#     available_agent = frappe.db.get_value("User", {"status": "Available", "user_type": "Agent"}, "phone") 

#     if available_agent:
#         return available_agent
#     else:
#         return None  # No agent available

@frappe.whitelist(allow_guest=True)
def handle_exotel_connect_request(**kwargs):
    """
    Handle the Exotel Connect applet GET request, 
    checking agent availability and returning appropriate response.
    """
    call_payload = kwargs
    agent_phone_number = fetch_emp_contact(call_payload.get("CallTo"))

    # if agent_phone_number:
        # response = { 
        #             "fetch_after_attempt":False,   
        #             "destination": {
        #                 "numbers":agent_phone_number
        #                 },
        #             "distribute_calls":"equally",
        #             "record": True,
        #             "recording_channels":"dual",
        #             "max_ringing_duration":75,
        #             "max_conversation_duration":3600,
        #             "dial_passthru_event_url":"https://construction.techbirdit.in/api/method/exotel_integration.handler.handle_request?key=67ec3e979522f4983b61",
        #             "music_on_hold": {"type":"default_tone"},
        #             }
    
    handle_request(**kwargs)
    frappe.response["fetch_after_attempt"] = False
    frappe.response["destination"] = {
                        "numbers":agent_phone_number
                        }
    frappe.response["distribute_calls"] = "equally"
    frappe.response["record"] = True
    frappe.response["recording_channels"] = "dual"
    frappe.response["max_ringing_duration"] = 20
    frappe.response["max_conversation_duration"] = 3600
    frappe.response["dial_passthru_event_url"] = "https://construction.techbirdit.in/api/method/exotel_integration.handler.handle_request?key=67ec3e979522f4983b61"
    frappe.response["music_on_hold"] = {"type":"default_tone"}
    frappe.log_error(message=str(frappe.response), title="Popup Error")
    
    
def fetch_emp_contact(comm_medium):
    today = datetime.combine(frappe.utils.now_datetime(), time(0, 0))
    on_call_log = frappe.get_all("Call Log", filters={"status":["is", "not set"], "type":"Incoming", "creation":[">",today]}, fields=["to"])
    emp_nos = []
    for no in on_call_log:
        emp_nos.append(no.to)
    
    frappe.log_error(message = str(emp_nos), title="On Call Employee Numbers")
    
    comm_med = frappe.get_doc("Communication Medium", comm_medium)
    # comm_med_items = frappe.get_value("Communication Medium Timeslot", {"day_of_week":frappe.utils.now_datetime().strftime('%A'), "from_time":["<="]})
    emp_group = frappe.get_all("Employee Group Table", filters={"parent" : comm_med.catch_all}, fields=["employee"])
    emp_ids = get_logged_in_employees(emp_group)
    
    all_emp_nos=[]
    for emp in emp_ids:
        if len(emp_group)>0:
            all_emp_nos.append(frappe.get_doc("Employee", emp).cell_number) 
    
    off_emp_nos = []
    for item in all_emp_nos:
        if item:
            number = ("0" + item) if not item.startswith('0') else item
            if number not in emp_nos:
                off_emp_nos.append(number)
    
    return off_emp_nos

def get_logged_in_employees(employee_ids):
    logged_in_employees = []
    sessions = frappe.db.sql("Select user from `tabSessions` where status = 'Active' ", as_dict=True)
    logged_in_users = set(session.user for session in sessions)
    emp_ids = [d['employee'] for d in employee_ids]
    
    employees = frappe.get_all('Employee', filters={'name': ['in', emp_ids]}, fields=['user_id',"name"])
    
    for employee in employees:
        if employee.user_id in logged_in_users:
            logged_in_employees.append(employee.name)
    
    return logged_in_employees