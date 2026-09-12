from django.test import TestCase, override_settings

from companies.models import Company

from .models import CompanyScope, HandbookEntry
from .retrieval import lexical_score, lexical_terms, search_rules


VECTOR = [0.1] * 1536
FAR_VECTOR = [-0.1] * 1536


@override_settings(
    HANDBOOK_HYBRID_RETRIEVAL_ENABLED=True,
    HANDBOOK_RETRIEVAL_CANDIDATE_MULTIPLIER=3,
    HANDBOOK_RETRIEVAL_VECTOR_WEIGHT=0.72,
)
class HybridRetrievalTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='HYBRID01')
        self.scope = CompanyScope.objects.create(
            company=self.company,
            kind=CompanyScope.Kind.PROJECT,
            name='payment-api',
        )

    def entry(self, title, body_en, vector=VECTOR):
        return HandbookEntry.objects.create(
            company=self.company,
            scope=self.scope,
            title=title,
            title_en=title,
            body_ko=body_en,
            body_en=body_en,
            status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.SLACK,
            embedding_ko=vector,
            embedding_en=vector,
        )

    def test_lexical_match_breaks_equal_vector_tie(self):
        self.entry('Annual leave', 'Register annual leave on the team calendar.')
        relevant = self.entry(
            'Friday deployment restriction',
            'Do not deploy payment-api on Friday afternoons.',
        )

        found = search_rules(
            VECTOR,
            self.company,
            [self.scope.id],
            limit=1,
            query='Can payment-api deploy on Friday?',
        )

        self.assertEqual(found, [relevant])
        self.assertGreater(found[0].retrieval_score, 0)

    def test_exact_keyword_can_rescue_a_weak_vector_candidate(self):
        self.entry('General deployment', 'Deploy services after approval.')
        relevant = self.entry(
            'payment-api rollback command',
            'Use payment-api rollback command ROLLBACK-42.',
            vector=FAR_VECTOR,
        )

        found = search_rules(
            VECTOR,
            self.company,
            [self.scope.id],
            limit=2,
            query='What is payment-api rollback command ROLLBACK-42?',
        )

        self.assertIn(relevant, found)

    def test_unembedded_rule_is_not_rescued(self):
        unembedded = self.entry(
            'payment-api secret command',
            'Use payment-api command SECRET-99.',
            vector=None,
        )

        found = search_rules(
            VECTOR,
            self.company,
            [self.scope.id],
            query='What is payment-api command SECRET-99?',
        )

        self.assertNotIn(unembedded, found)

    def test_vector_only_call_keeps_the_existing_contract(self):
        entry = self.entry('Deploy', 'Deploy after approval.')

        found = search_rules(VECTOR, self.company, [self.scope.id])

        self.assertEqual(found, [entry])
        self.assertTrue(hasattr(found[0], 'distance'))

    def test_lexical_helpers_ignore_common_question_words(self):
        self.assertEqual(
            lexical_terms('Can I deploy payment-api on Friday?'),
            ['deploy', 'payment-api', 'friday'],
        )
        self.assertEqual(
            lexical_score('rollback ROLLBACK-42', 'Use command ROLLBACK-42 to rollback.'),
            1.0,
        )
