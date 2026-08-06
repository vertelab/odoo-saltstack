# -*- coding: utf-8 -*-
"""saltstack_ai — tester för ai-tool-access-capabilities (referensimpl.).

Körs med: checkmodule -d <db> -m saltstack_ai -t
Täcker: 5.7 körbarhet (kompilering), 5.8 access (operator vs icke-operatör),
5.9 förmågeserialisering (salt enum-delning, zabbix namespace),
5.10 access-filtrerad medlem dold i enum.
"""

from odoo.tests.common import TransactionCase

SALT_TOOL_IDS = [
    'tool_salt_test_ping', 'tool_salt_state_apply', 'tool_salt_state_highstate',
    'tool_salt_state_show_sls', 'tool_salt_cmd_run', 'tool_salt_pillar_get',
    'tool_salt_pillar_items', 'tool_salt_grains_get', 'tool_salt_grains_items',
    'tool_salt_minion_list', 'tool_salt_minion_accept',
    'tool_salt_service_status', 'tool_salt_service_restart',
    'tool_salt_disk_usage', 'tool_salt_memory_usage', 'tool_salt_system_load',
    'tool_salt_journal_errors', 'tool_salt_process_list',
]
ZABBIX_TOOL_IDS = [
    'tool_zabbix_get_alerts', 'tool_zabbix_get_host', 'tool_zabbix_get_problems',
    'tool_zabbix_acknowledge', 'tool_zabbix_get_history', 'tool_zabbix_get_item',
    'tool_zabbix_get_host_groups', 'tool_zabbix_get_triggers',
]
ALL_TOOL_IDS = SALT_TOOL_IDS + ZABBIX_TOOL_IDS + ['tool_driftlarm_update']


def _all_tool_records(env):
    """Alla salt/zabbix/driftlarm ai.tool-poster via xmlid (namn är inte xmlid)."""
    return env['ai.tool'].browse([
        env.ref('saltstack_ai.%s' % x).id for x in ALL_TOOL_IDS])



class TestSaltstackToolsExecutable(TransactionCase):

    def test_all_tools_compile(self):
        """5.7: alla 26+1 verktyg har kompilerbar kod."""
        for xmlid in ALL_TOOL_IDS:
            tool = self.env.ref('saltstack_ai.%s' % xmlid)
            if tool.executor == 'nats':
                continue
            self.assertTrue(
                tool.code and tool.code.strip(),
                '%s saknar kod' % xmlid)
            compile(tool.code, '<%s>' % xmlid, 'exec')


class TestSaltstackAccessGroups(TransactionCase):

    def setUp(self):
        super().setUp()
        self.operator_group = self.env.ref(
            'saltstack_ai.group_infra_operator')
        self.tools = _all_tool_records(self.env)

    def test_tools_bound_to_operator_group(self):
        """5.8: alla verktyg är bundna till operatorgruppen."""
        self.assertEqual(len(self.tools), len(ALL_TOOL_IDS))
        for tool in self.tools:
            self.assertIn(
                self.operator_group, tool.group_ids,
                '%s saknar operatorgrupp' % tool.name)

    def test_non_operator_sees_none(self):
        """5.8: användare utan grupp ser inga salt/zabbix-verktyg."""
        visible = self.tools._filter_by_access_groups([])
        self.assertEqual(len(visible), 0)

    def test_operator_sees_all(self):
        """5.8: operatör ser alla; läs utan HITL, destruktiva med."""
        visible = self.tools._filter_by_access_groups(
            [self.operator_group.id])
        self.assertEqual(len(visible), len(ALL_TOOL_IDS))
        read_tool = self.env.ref('saltstack_ai.tool_salt_test_ping')
        act_tool = self.env.ref('saltstack_ai.tool_salt_state_apply')
        # Läs → ingen HITL; destruktiv → alltid godkännande
        self.assertFalse(read_tool.risk_level in ('destructive', 'execute'))
        self.assertEqual(act_tool.risk_level, 'destructive')

    def test_coworker_has_operator_group(self):
        """5.8/5.4: Infrastructure Operator har operatorgruppen."""
        coworker = self.env.ref(
            'saltstack_ai.coworker_infrastructure_operator')
        self.assertIn(self.operator_group, coworker.group_ids)


class TestSaltstackCapabilities(TransactionCase):

    def setUp(self):
        super().setUp()
        self.salt_cap = self.env.ref('saltstack_ai.cap_salt_ops')
        self.zabbix_cap = self.env.ref('saltstack_ai.cap_zabbix_ops')
        self.operator_group = self.env.ref(
            'saltstack_ai.group_infra_operator')

    def _registry_from_caps(self, cap, mode, group_ids):
        """Bygg registry med access-filtrerade medlemmar + applicera läge."""
        from odoo.addons.ai_agent_core.core.tools import (
            ToolRegistry, apply_capability_serialization,
            ai_tool_records_to_tools)
        members = cap.member_ids._filter_by_access_groups(group_ids)
        reg = ToolRegistry()
        reg.register_many(ai_tool_records_to_tools(members, self.env))
        capabilities = [{
            'name': cap.name,
            'description': cap.description,
            'member_names': [m.name for m in members],
        }]
        suffix = apply_capability_serialization(reg, capabilities, mode)
        return reg, suffix

    def test_salt_capability_enum_splits(self):
        """5.9: salt (18) > 8 → delas i ≤8-operationers enheter."""
        reg, _ = self._registry_from_caps(
            self.salt_cap, 'enum', [self.operator_group.id])
        enum_tools = [t for t in reg.list() if t.name.startswith('salt')]
        self.assertEqual(len(enum_tools), 3)  # 18/8 → 3 enheter
        from odoo.addons.ai_agent_core.core.tools import (
            CAPABILITY_ENUM_MAX_OPS)
        for t in enum_tools:
            ops = t.parameters['properties']['operation']['enum']
            self.assertLessEqual(len(ops), CAPABILITY_ENUM_MAX_OPS)
        total_ops = sum(len(
            t.parameters['properties']['operation']['enum'])
            for t in enum_tools)
        self.assertEqual(total_ops, len(self.salt_cap.member_ids))

    def test_zabbix_capability_namespace(self):
        """5.9: zabbix (8) i namespace visar medlemmar + beskrivning."""
        reg, suffix = self._registry_from_caps(
            self.zabbix_cap, 'namespace', [self.operator_group.id])
        self.assertIn('zabbix', suffix)
        # Individuella verktyg finns kvar
        names = [t.name for t in reg.list()]
        for m in self.zabbix_cap.member_ids:
            self.assertIn(m.name, names)

    def test_access_filtered_member_hidden_in_enum(self):
        """5.10: medlem utan access dold som operation i enum."""
        # Skapa en extra grupp och ersätt zabbix_acknowledge's grupper med den
        other = self.env['res.groups'].create({
            'name': 'Zabbix Ack Only',
            'category_id': self.env.ref('base.module_category_hidden').id,
        })
        ack = self.env.ref('saltstack_ai.tool_zabbix_acknowledge')
        ack.write({'group_ids': [(6, 0, [other.id])]})
        # Användare i operatorgruppen men INTE i 'other' → ack dold
        reg, _ = self._registry_from_caps(
            self.zabbix_cap, 'enum', [self.operator_group.id])
        enum_tool = reg.get('zabbix')
        self.assertIsNotNone(enum_tool)
        ops = enum_tool.parameters['properties']['operation']['enum']
        self.assertNotIn('zabbix_acknowledge', ops)
        self.assertIn('zabbix_get_alerts', ops)
