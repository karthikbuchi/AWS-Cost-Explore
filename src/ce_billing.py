from collections import defaultdict
import boto3
import datetime
import urllib3
import os, logging, json


LOGGER_LEVEL_STRING = os.environ.get('LOGGER_LEVEL_STRING', "DEBUG")

LOGGER = logging.getLogger(__name__)
LOGGER.info("LOGGING LEVEL: " + str(LOGGER_LEVEL_STRING))

NUMBER_OF_ITEMS = int(os.environ.get('NUMBER_OF_ITEMS', 1))
LOGGER.info("NUMBER_OF_ITEMS: " + str(NUMBER_OF_ITEMS))



LOGGER.setLevel(LOGGER_LEVEL_STRING)

n_days = 7
yesterday = datetime.datetime.today() - datetime.timedelta(days=1)
week_ago = yesterday - datetime.timedelta(days=n_days)

# It seems that the sparkline symbols don't line up (probalby based on font?) so put them last
# Also, leaving out the full block because Slack doesn't like it: '█'
sparks = ['▁', '▂', '▃', '▄', '▅', '▆', '▇']

def sparkline(datapoints):
    lower = min(datapoints)
    upper = max(datapoints)
    width = upper - lower
    n_sparks = len(sparks) - 1

    line = ""
    for dp in datapoints:
        scaled = 1 if width == 0 else (dp - lower) / width
        which_spark = int(scaled * n_sparks)
        line += (sparks[which_spark])

    return line


def delta(costs):
    if (len(costs) > 1 and costs[-1] >= 1 and costs[-2] >= 1):
        # This only handles positive numbers
        result = ((costs[-1] / costs[-2]) - 1) * 100.0
    else:
        result = 0
    return result


def report_cost(event, context, result: dict = None, yesterday: str = None, new_method=True):

    if yesterday is None:
        yesterday = datetime.datetime.today() - datetime.timedelta(days=1)
    else:
        yesterday = datetime.datetime.strptime(yesterday, '%Y-%m-%d')

    week_ago = yesterday - datetime.timedelta(days=n_days)
    # Generate list of dates, so that even if our data is sparse,
    # we have the correct length lists of costs (len is n_days)
    list_of_dates = [
        (week_ago + datetime.timedelta(days=x)).strftime('%Y-%m-%d')
        for x in range(n_days)
    ]
    # print(list_of_dates)

    # Get account account name from env, or account id/account alias from boto3
    account_name = os.environ.get("AWS_ACCOUNT_NAME", None)
    if account_name is None:
        iam = boto3.client("iam")
        paginator = iam.get_paginator("list_account_aliases")
        for aliases in paginator.paginate(PaginationConfig={"MaxItems": 1}):
            if "AccountAliases" in aliases and len(aliases["AccountAliases"]) > 0:
                account_name = aliases["AccountAliases"][0]

    if account_name is None:
        account_name = boto3.client("sts").get_caller_identity().get("Account")

    if account_name is None:
        account_name = "[NOT FOUND]"

    number_of_services = event.get("number_of_items", NUMBER_OF_ITEMS)

    client = boto3.client('ce')

    query = {
        "TimePeriod": {
            "Start": week_ago.strftime('%Y-%m-%d'),
            "End": yesterday.strftime('%Y-%m-%d'),
        },
        "Granularity": "DAILY",
        "Filter": {
            "Not": {
                "Dimensions": {
                    "Key": "RECORD_TYPE",
                    "Values": [
                        "Credit",
                        "Refund",
                        "Upfront",
                        "Support",
                    ]
                }
            }
        },
        "Metrics": ["UnblendedCost"],
        "GroupBy": [
            {
                "Type": "DIMENSION",
                "Key": "SERVICE",
            },
        ],
    }

    # Only run the query when on lambda, not when testing locally with example json
    if result is None:
        result = client.get_cost_and_usage(**query)

    cost_per_day_by_service = defaultdict(list)

    if new_method == False:
        # Build a map of service -> array of daily costs for the time frame
        for day in result['ResultsByTime']:
            for group in day['Groups']:
                key = group['Keys'][0]
                cost = float(group['Metrics']['UnblendedCost']['Amount'])
                cost_per_day_by_service[key].append(cost)
    else:
        # New method, which first creates a dict of dicts
        # then loop over the services and loop over the list_of_dates
        # and this means even for sparse data we get a full list of costs
        cost_per_day_dict = defaultdict(dict)

        for day in result['ResultsByTime']:
            start_date = day["TimePeriod"]["Start"]
            for group in day['Groups']:
                key = group['Keys'][0]
                cost = float(group['Metrics']['UnblendedCost']['Amount'])
                cost_per_day_dict[key][start_date] = cost

        for key in cost_per_day_dict.keys():
            for start_date in list_of_dates:
                cost = cost_per_day_dict[key].get(start_date, 0.0)  # fallback for sparse data
                cost_per_day_by_service[key].append(cost)

    # Sort the map by yesterday's cost
    most_expensive_yesterday = sorted(cost_per_day_by_service.items(), key=lambda i: i[1][-1], reverse=True)

    service_names = [k for k, _ in most_expensive_yesterday[:number_of_services]]
    longest_name_len = len(max(service_names, key=len))

    buffer = f"{'Service':{longest_name_len}} ${'Yday':8} {'∆%':>5} {'Last 7d':7}\n"

    for service_name, costs in most_expensive_yesterday[:number_of_services]:
        buffer += f"{service_name:{longest_name_len}} ${costs[-1]:8,.2f} {delta(costs):4.0f}% {sparkline(costs):7}\n"

    other_costs = [0.0] * n_days
    for service_name, costs in most_expensive_yesterday[number_of_services:]:
        for i, cost in enumerate(costs):
            other_costs[i] += cost

    buffer += f"{'Other':{longest_name_len}} ${other_costs[-1]:8,.2f} {delta(other_costs):4.0f}% {sparkline(other_costs):7}\n"

    total_costs = [0.0] * n_days
    for day_number in range(n_days):
        for service_name, costs in most_expensive_yesterday:
            try:
                total_costs[day_number] += costs[day_number]
            except IndexError:
                total_costs[day_number] += 0.0

    buffer += f"{'Total':{longest_name_len}} ${total_costs[-1]:8,.2f} {delta(total_costs):4.0f}% {sparkline(total_costs):7}\n"

    cost_per_day_by_service["total"] = total_costs[-1]

    credits_expire_date = os.environ.get('CREDITS_EXPIRE_DATE')
    if credits_expire_date:
        credits_expire_date = datetime.datetime.strptime(credits_expire_date, "%m/%d/%Y")

        credits_remaining_as_of = os.environ.get('CREDITS_REMAINING_AS_OF')
        credits_remaining_as_of = datetime.datetime.strptime(credits_remaining_as_of, "%m/%d/%Y")

        credits_remaining = float(os.environ.get('CREDITS_REMAINING'))

        days_left_on_credits = (credits_expire_date - credits_remaining_as_of).days
        allowed_credits_per_day = credits_remaining / days_left_on_credits

        relative_to_budget = (total_costs[-1] / allowed_credits_per_day) * 100.0

        if relative_to_budget < 60:
            emoji = ":white_check_mark:"
        elif relative_to_budget > 110:
            emoji = ":rotating_light:"
        else:
            emoji = ":warning:"

        summary = (f"{emoji} Yesterday's cost for {account_name} ${total_costs[-1]:,.2f} "
                   f"is {relative_to_budget:.2f}% of credit budget "
                   f"${allowed_credits_per_day:,.2f} for the day."
                   )
    else:
        summary = f"Yesterday's cost for account {account_name} was ${total_costs[-1]:,.2f}"

    # hook_url = event.get("webhook_url", TEAMS_WEBHOOK_URL)
    hook_url = None
    if hook_url:
        http = urllib3.PoolManager()
        r = http.request('POST', hook_url,
                         headers={'Content-Type': 'application/json'},
                         body=json.dumps({
                             "text": summary + "\n\n```\n" + buffer + "\n```",
                         }))

        if r.status != 200:
            print("HTTP %s: %s" % (r.status, r.text))
    else:
        pass
        
    s3 = boto3.resource('s3')
    bucket = os.environ.get('BUCKET', 'ciexchange')
    upload = s3.Bucket(bucket)

    with open('/tmp/summary.txt', 'a') as data:
        data.write(summary + "\n")
        data.close
        data.write(buffer)
    filename = datetime.datetime.now().strftime("%Y_%m_%d")
    
    upload.upload_file('/tmp/summary.txt', f"report/{filename}.txt")
    
    # for running locally to test output
    return cost_per_day_by_service, summary, buffer