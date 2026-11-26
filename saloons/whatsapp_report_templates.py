"""
Monthly salon report — WhatsApp Utility templates.

These are the exact template definitions submitted to Meta. The body text is
fixed once Meta approves it; Elegentra fills the numbered blanks each month
(see saloons/monthly_report.py). Keep the blank order in sync with
`report_parameters()` there.

Written as account statements to fit WhatsApp's Utility category: no thanks,
relationship lines or growth tips (Meta treats those as Marketing).
"""

# (The first versions, elegentra_first_month_report / elegentra_monthly_report_up /
# elegentra_monthly_report_down, were classed as Marketing and can't be re-categorised.)
TEMPLATE_FIRST_MONTH = "elegentra_salon_report_first"
TEMPLATE_UP = "elegentra_salon_report_up"
TEMPLATE_DOWN = "elegentra_salon_report_down"
LANGUAGE = "en_US"

_STATS_LINES = (
    "👀 {{4}} visited your salon page\n"
    "📍 {{5}} checked your location\n"
    "💬 {{6}} · 📞 {{7}}\n"
    "🔖 {{8}} saved your salon\n"
    "✂️ Most popular service: {{9}}\n"
    "⭐ {{10}}"
)

_TURN_OFF = "You can turn these reports off in Dashboard → Settings."

_BODY_FIRST_MONTH = (
    "Hi {{1}}, your Elegentra account report for {{2}} is ready.\n\n"
    "Activity on your salon page in {{3}}:\n\n"
    f"{_STATS_LINES}\n\n"
    "This is your first monthly report. You'll get one each month while your salon is listed "
    "on Elegentra. You can turn it off in Dashboard → Settings."
)

_BODY_UP = (
    "Hi {{1}}, your monthly Elegentra account report for {{2}} is ready.\n\n"
    "Activity on your salon page in {{3}}:\n\n"
    f"{_STATS_LINES}\n\n"
    "📊 Compared with {{11}}: {{12}}\n\n"
    f"Full details are in your dashboard. {_TURN_OFF}"
)

_BODY_DOWN = (
    "Hi {{1}}, your monthly Elegentra account report for {{2}} is ready.\n\n"
    "Activity on your salon page in {{3}}:\n\n"
    f"{_STATS_LINES}\n\n"
    "📊 Compared with {{11}}: {{12}}\n\n"
    f"Visits were lower than last month. Full details are in your dashboard. {_TURN_OFF}"
)

_EXAMPLE_STATS = [
    "Rahul",
    "Glow Salon",
    "December",
    "154 people",
    "42 people",
    "19 WhatsApp enquiries",
    "8 calls",
    "12 customers",
    "Bridal makeup · 21 people interested",
    "3 new reviews · 4.7★ average",
]


def _definition(name, body, examples):
    return {
        "name": name,
        "language": LANGUAGE,
        "category": "UTILITY",
        "components": [
            {"type": "BODY", "text": body, "example": {"body_text": [examples]}},
            {"type": "FOOTER", "text": "Team Elegentra"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL",
                        "text": "Open dashboard",
                        "url": "https://elegentra.com/saloons/dashboard/{{1}}",
                        "example": ["https://elegentra.com/saloons/dashboard/glowsalon"],
                    }
                ],
            },
        ],
    }


TEMPLATE_DEFINITIONS = [
    _definition(TEMPLATE_FIRST_MONTH, _BODY_FIRST_MONTH, _EXAMPLE_STATS[:2] + ["November"] + _EXAMPLE_STATS[3:]),
    _definition(TEMPLATE_UP, _BODY_UP, _EXAMPLE_STATS + ["November", "+30 visits (124 → 154)"]),
    _definition(
        TEMPLATE_DOWN,
        _BODY_DOWN,
        _EXAMPLE_STATS[:3]
        + ["96 people", "22 people", "9 WhatsApp enquiries", "4 calls", "5 customers",
           "No service interest yet", "No new reviews this month", "November", "−12 visits (108 → 96)"],
    ),
]
