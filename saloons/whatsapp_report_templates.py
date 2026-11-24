"""
Monthly salon report — WhatsApp Utility templates.

These are the exact template definitions submitted to Meta. The body text is
fixed once Meta approves it; Elegentra fills the numbered blanks each month
(see saloons/monthly_report.py). Keep the blank order in sync with
`report_parameters()` there.
"""

TEMPLATE_FIRST_MONTH = "elegentra_first_month_report"
TEMPLATE_UP = "elegentra_monthly_report_up"
TEMPLATE_DOWN = "elegentra_monthly_report_down"
LANGUAGE = "en_US"

_STATS_LINES = (
    "👀 {{4}} visited your salon page\n"
    "📍 {{5}} checked your location\n"
    "💬 {{6}} · 📞 {{7}}\n"
    "🔖 {{8}} saved your salon\n"
    "✂️ Most popular service: {{9}}\n"
    "⭐ {{10}}"
)

_CLOSING = "We're with you every step of the way 🌸"

_BODY_FIRST_MONTH = (
    "Hi {{1}} 👋\n\n"
    "Your first month with Elegentra is complete, and we're happy to have {{2}} with us 💛\n\n"
    "Here's how customers discovered you in {{3}}:\n\n"
    f"{_STATS_LINES}\n\n"
    f"{_CLOSING}"
)

_BODY_UP = (
    "Hi {{1}} 👋\n\n"
    "Here's how {{2}} did on Elegentra in {{3}}. Thank you for being with us 💛\n\n"
    f"{_STATS_LINES}\n\n"
    "📊 Compared with {{11}}: {{12}}\n\n"
    f"{_CLOSING}"
)

_BODY_DOWN = (
    "Hi {{1}} 👋\n\n"
    "Here's how {{2}} did on Elegentra in {{3}}. Thank you for being with us 💛\n\n"
    f"{_STATS_LINES}\n\n"
    "📊 Compared with {{11}}: {{12}}\n\n"
    "It was a quieter month, and that's normal. 💡 Tip: salons with recent photos and "
    "up-to-date prices usually get more visits.\n\n"
    f"{_CLOSING}"
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
