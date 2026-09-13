PROJECTS = (
    'Atlas', 'Boreal', 'Cygnus', 'Delta', 'Ember',
)


GROUPS = (
    {
        'key': 'deploy',
        'title': '{project} production deployment window',
        'body': (
            '{project} production deployments are permitted only on {day} '
            'between {window}. Announce them in {channel} at least {notice} '
            'minutes beforehand.'
        ),
        'items': (
            ('Tuesday', '14:00-16:00 UTC', '#atlas-release', '30'),
            ('Wednesday', '09:00-11:00 UTC', '#boreal-release', '45'),
            ('Monday', '16:00-18:00 UTC', '#cygnus-release', '20'),
            ('Thursday', '13:00-15:00 UTC', '#delta-release', '60'),
            ('Tuesday', '08:00-10:00 UTC', '#ember-release', '25'),
        ),
        'fields': ('day', 'window', 'channel', 'notice'),
        'facts': ('{project}', '{day}', '{window}', '{channel}', '{notice} minutes'),
        'questions': (
            'What is the production deployment window and notice requirement for {project}?',
            'For {project}, when may we deploy and where must it be announced?',
            '{project} release window, channel, and advance-notice policy?',
        ),
    },
    {
        'key': 'review',
        'title': '{project} database pull request review',
        'body': (
            'A {project} pull request containing a database migration requires '
            '{approvals} approvals, including one approval from {role}.'
        ),
        'items': (
            ('2', 'the data lead'), ('3', 'the platform lead'),
            ('2', 'the service owner'), ('3', 'the database owner'),
            ('2', 'the backend lead'),
        ),
        'fields': ('approvals', 'role'),
        'facts': ('{project}', '{approvals} approvals', '{role}'),
        'questions': (
            'What reviews are required for a database migration PR in {project}?',
            'Before merging a {project} migration, how many approvals and which role are needed?',
            '{project} database-change PR approval policy?',
        ),
    },
    {
        'key': 'incident',
        'title': '{project} critical incident response',
        'body': (
            'For a critical {project} incident, page {role} within {minutes} '
            'minutes and open the {channel} incident channel.'
        ),
        'items': (
            ('the primary on-call engineer', '5', '#atlas-incident'),
            ('the reliability lead', '10', '#boreal-incident'),
            ('the service owner', '7', '#cygnus-incident'),
            ('the security on-call engineer', '4', '#delta-incident'),
            ('the platform manager', '8', '#ember-incident'),
        ),
        'fields': ('role', 'minutes', 'channel'),
        'facts': ('{project}', '{role}', '{minutes} minutes', '{channel}'),
        'questions': (
            'How must a critical incident be escalated for {project}?',
            'Who is paged, by when, and which channel is opened for a {project} incident?',
            '{project} critical-incident contact, SLA, and channel?',
        ),
    },
    {
        'key': 'retention',
        'title': '{project} customer data retention',
        'body': (
            '{project} customer export files must be deleted after {days} days '
            'and stored only in {storage} while active.'
        ),
        'items': (
            ('14', 'the encrypted Atlas vault'),
            ('21', 'the Boreal restricted bucket'),
            ('30', 'the Cygnus secure workspace'),
            ('10', 'the Delta compliance drive'),
            ('7', 'the Ember encrypted bucket'),
        ),
        'fields': ('days', 'storage'),
        'facts': ('{project}', '{days} days', '{storage}'),
        'questions': (
            'How long may {project} customer exports be retained, and where?',
            'State the storage location and deletion deadline for {project} customer export files.',
            '{project} export retention period and approved storage?',
        ),
    },
    {
        'key': 'invoice',
        'title': '{project} invoice approval threshold',
        'body': (
            '{project} invoices above {amount} require approval from {role} '
            'before payment is scheduled.'
        ),
        'items': (
            ('$1,500', 'the finance lead'), ('$2,000', 'the project owner'),
            ('$750', 'the operations manager'), ('$5,000', 'the CFO'),
            ('$1,250', 'the procurement lead'),
        ),
        'fields': ('amount', 'role'),
        'facts': ('{project}', '{amount}', '{role}'),
        'questions': (
            'What is the invoice approval threshold and approver for {project}?',
            'Who must approve a {project} invoice, and above what amount?',
            '{project} payment scheduling approval limit?',
        ),
    },
    {
        'key': 'access',
        'title': '{project} privileged access review',
        'body': (
            '{project} administrator access is reviewed every {cadence} by '
            '{role}; unused access must be removed within {hours} hours.'
        ),
        'items': (
            ('30 days', 'the security lead', '24'),
            ('14 days', 'the platform owner', '12'),
            ('60 days', 'the compliance manager', '48'),
            ('7 days', 'the security on-call engineer', '8'),
            ('45 days', 'the infrastructure lead', '24'),
        ),
        'fields': ('cadence', 'role', 'hours'),
        'facts': ('{project}', '{cadence}', '{role}', '{hours} hours'),
        'questions': (
            'How is privileged access reviewed and removed for {project}?',
            'For {project}, who reviews administrator access, how often, and how quickly is unused access removed?',
            '{project} admin-access audit cadence and revocation SLA?',
        ),
    },
    {
        'key': 'onboarding',
        'title': '{project} new hire equipment onboarding',
        'body': (
            'For a new hire joining {project}, {role} must submit the equipment '
            'request {days} business days before the start date.'
        ),
        'items': (
            ('the hiring manager', '5'), ('the team coordinator', '7'),
            ('the engineering manager', '4'), ('the people partner', '10'),
            ('the project lead', '6'),
        ),
        'fields': ('role', 'days'),
        'facts': ('{project}', '{role}', '{days} business days'),
        'questions': (
            'Who requests equipment for a {project} new hire, and by when?',
            'What is the lead time and responsible role for {project} onboarding equipment?',
            '{project} new-starter equipment request deadline?',
        ),
    },
    {
        'key': 'meeting',
        'title': '{project} remote decision meeting',
        'body': (
            'The {project} remote decision meeting occurs every {day} at {time}; '
            'decisions must be recorded in {location}.'
        ),
        'items': (
            ('Monday', '09:30 UTC', 'the Atlas decision log'),
            ('Tuesday', '15:00 UTC', 'the Boreal wiki'),
            ('Wednesday', '11:00 UTC', 'the Cygnus RFC board'),
            ('Thursday', '16:30 UTC', 'the Delta decision register'),
            ('Friday', '08:30 UTC', 'the Ember project log'),
        ),
        'fields': ('day', 'time', 'location'),
        'facts': ('{project}', '{day}', '{time}', '{location}'),
        'questions': (
            'When is the remote decision meeting for {project}, and where are decisions recorded?',
            'Give the {project} decision meeting schedule and documentation location.',
            '{project} remote meeting time and decision log?',
        ),
    },
    {
        'key': 'rollback',
        'title': '{project} release rollback trigger',
        'body': (
            'Roll back a {project} release when the {metric} exceeds {threshold} '
            'for {minutes} consecutive minutes, then notify {channel}.'
        ),
        'items': (
            ('error rate', '3%', '5', '#atlas-release'),
            ('checkout failure rate', '2%', '10', '#boreal-release'),
            ('API timeout rate', '4%', '7', '#cygnus-release'),
            ('authentication failure rate', '1%', '3', '#delta-release'),
            ('job failure rate', '5%', '8', '#ember-release'),
        ),
        'fields': ('metric', 'threshold', 'minutes', 'channel'),
        'facts': ('{project}', '{metric}', '{threshold}', '{minutes} consecutive minutes', '{channel}'),
        'questions': (
            'What triggers a release rollback for {project}, and who is notified?',
            'For {project}, identify the metric, threshold, duration, and notification channel for rollback.',
            '{project} rollback threshold and alert destination?',
        ),
    },
)


UNSUPPORTED_CASES = (
    ('travel-class', 'What travel class may employees book for international trips?', 'NO_SOURCE', True),
    ('meal-budget', 'What is the daily meal reimbursement budget?', 'NO_SOURCE', True),
    ('office-pets', 'Are pets allowed in the office?', 'NO_SOURCE', True),
    ('conference-budget', 'How much conference attendance budget does each employee receive?', 'NO_SOURCE', True),
    ('hardware-refresh', 'How often are employee laptops replaced?', 'NO_SOURCE', True),
    ('parental-leave', 'How many weeks of parental leave are provided?', 'NO_SOURCE', True),
    ('taxi-policy', 'When may an employee expense a taxi home?', 'NO_SOURCE', True),
    ('training-days', 'How many paid training days are available each year?', 'NO_SOURCE', True),
    ('bonus-cycle', 'When are annual performance bonuses paid?', 'NO_SOURCE', True),
    ('phone-stipend', 'What is the monthly mobile phone stipend?', 'NO_SOURCE', True),
    ('weather', 'Will it rain in Seoul tomorrow?', 'OUT_OF_SCOPE', False),
    ('recipe', 'How do I make tomato pasta sauce?', 'OUT_OF_SCOPE', False),
    ('movie', 'Which science fiction movie should I watch tonight?', 'OUT_OF_SCOPE', False),
    ('exercise', 'What exercise is best for improving my marathon time?', 'OUT_OF_SCOPE', False),
    ('tourism', 'Which museum should I visit during a holiday in Paris?', 'OUT_OF_SCOPE', False),
)


def build_rules():
    rules = []
    for group in GROUPS:
        for project, values in zip(PROJECTS, group['items']):
            context = {'project': project}
            context.update(dict(zip(group['fields'], values)))
            rules.append({
                'key': f'{project.lower()}-{group["key"]}',
                'category': group['key'],
                'title': group['title'].format(**context),
                'body': group['body'].format(**context),
                'questions': [
                    question.format(**context) for question in group['questions']
                ],
                'facts': [fact.format(**context) for fact in group['facts']],
            })
    return tuple(rules)


RULES = build_rules()
