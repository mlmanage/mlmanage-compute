#!/usr/bin/env python3
"""Offline tests for GPU sharing (time-slicing / MPS) actually reaching the node.

Regression coverage for per-card GPU sharing configuration reaching the node. Three areas:

* the committed deploy default in gpu-operator/time-slicing-config.yaml, and that the
  ClusterPolicy in deploy-all.sh names the same default profile key;
* `_put_plugin_profile()` in api/main.py, which must ADD a profile to the device-plugin
  ConfigMap without destroying the deploy-time default or any other node's profile — the
  original bug rebuilt the ConfigMap from scratch, so those nodes' labels pointed at a key
  that no longer existed and their capacity never changed;
* `apply_device_sharing()`, which builds the per-node profile from ALL of that node's
  partition records, so splitting one card neither un-splits its siblings nor silently
  splits the cards that were never asked for.

api/main.py cannot be imported here (it needs fastapi/kubernetes/sqlalchemy, which are not
installed in the WSL dev environment and the cluster serves main.py from a ConfigMap), so
the functions under test are lifted out with `ast` and executed against a fake CoreV1Api.
That keeps the assertions against the real source rather than a copy.
"""

import ast
import os
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAIN = Path(os.environ.get("MLM_MAIN_PATH", REPO / "devops-backend" / "api" / "main.py"))
TS_CONFIG = REPO / "devops-backend" / "gpu-operator" / "time-slicing-config.yaml"
DEPLOY = REPO / "devops-backend" / "scripts" / "deploy-all.sh"

# Profile key that means "no sharing"; must match the `default` key written by main.py.
DEFAULT_KEY = "default"
CONFIG_LABEL = "nvidia.com/device-plugin.config"
# Prefix of the keys MLManage owns; must match MANAGED_PLUGIN_CONFIG_PREFIX in main.py.
MANAGED_PREFIX = "mlm-"
EXTRACT = ("_put_plugin_profile", "apply_device_sharing")


class FakeApiException(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"status={status}")


class FakeHTTPException(Exception):
    def __init__(self, status_code=None, detail=None):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class _CM:
    def __init__(self, data):
        self.data = data


class FakeCoreV1Api:
    """Minimal CoreV1Api that records calls, so ordering and verb choice are assertable."""

    def __init__(self, cm_data=None, cm_missing=False):
        self.cm = None if cm_missing else _CM(dict(cm_data or {}))
        self.calls = []
        self.node_labels = {}

    def read_namespaced_config_map(self, name, namespace):
        self.calls.append("read_cm")
        if self.cm is None:
            raise FakeApiException(404)
        return self.cm

    def patch_namespaced_config_map(self, name, namespace, body):
        self.calls.append("patch_cm")
        if self.cm is None:
            raise FakeApiException(404)
        self.cm.data.update(body["data"])  # strategic merge, as the real API does
        return self.cm

    def replace_namespaced_config_map(self, name, namespace, body):
        # Destructive: drops keys absent from body. The fix must not use this.
        self.calls.append("replace_cm")
        self.cm = _CM(dict(body["data"]))
        return self.cm

    def create_namespaced_config_map(self, namespace, body):
        self.calls.append("create_cm")
        self.cm = _CM(dict(body["data"]))
        return self.cm

    def patch_node(self, name, body):
        self.calls.append("patch_node")
        self.node_labels.setdefault(name, {}).update(body["metadata"]["labels"])


class _Record:
    """Stand-in for a GPUPartitionDB row."""

    def __init__(self, gpu_uuid, mode, replicas, node="nodeA"):
        self.gpu_uuid = gpu_uuid
        self.mode = mode
        self.replicas = replicas
        self.node = node


class _Query:
    def __init__(self, records):
        self._records = records

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._records


class FakeDB:
    def __init__(self, records):
        self._records = records

    def query(self, model):
        return _Query(self._records)


def load_sharing_functions(fake, index_map=None):
    """Exec the real sharing helpers from api/main.py against a fake Kubernetes client."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in EXTRACT]
    missing = set(EXTRACT) - {n.name for n in picked}
    if missing:
        raise AssertionError(f"api/main.py no longer defines: {sorted(missing)}")

    kubernetes = types.ModuleType("kubernetes")
    client = types.ModuleType("kubernetes.client")
    exceptions = types.ModuleType("kubernetes.client.exceptions")
    exceptions.ApiException = FakeApiException
    client.exceptions = exceptions
    client.CoreV1Api = lambda: fake
    kubernetes.client = client

    indices = dict(index_map or {})
    ns = {
        "kubernetes": kubernetes,
        "load_kubernetes_config": lambda: None,
        "TIME_SLICING_CONFIGMAP": "time-slicing-config",
        "GPU_OPERATOR_NS": "gpu-operator",
        "MANAGED_PLUGIN_CONFIG_PREFIX": MANAGED_PREFIX,
        "GPUPartitionDB": types.SimpleNamespace(node=object()),
        "HTTPException": FakeHTTPException,
        "gpu_device_index": lambda node, uuid: indices.get(uuid),
    }
    for node in picked:
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(MAIN), "exec"), ns)
    return ns


def _require_yaml():
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML ships with the test environment
        raise unittest.SkipTest("PyYAML is not installed")
    return yaml


@unittest.skipUnless(MAIN.exists(), "api/main.py is not present in this focused-test copy")
class SharingConfigMapTests(unittest.TestCase):
    """Writing one node's profile must not destroy anybody else's."""

    def apply(self, records, node="nodeA", index_map=None, **kw):
        fake = FakeCoreV1Api(**kw)
        if index_map is None:
            index_map = {r.gpu_uuid: i for i, r in enumerate(records)}
        ns = load_sharing_functions(fake, index_map)
        result = ns["apply_device_sharing"](node, FakeDB(records))
        return fake, result

    def test_deploy_time_default_profile_is_never_overwritten(self):
        # The bug: 'default' was rewritten to a bare "version: v1" on every save, so an
        # operator-tuned default profile silently reverted to no-sharing.
        deploy_default = "version: v1\n# tuned by the deploy\n"
        fake, _ = self.apply([_Record("gpu-nodeA-0", "timeslice", 4)],
                             cm_data={DEFAULT_KEY: deploy_default})
        self.assertEqual(deploy_default, fake.cm.data[DEFAULT_KEY])

    def test_other_nodes_profiles_survive_a_save(self):
        # The bug: saving nodeA's profile deleted nodeB's, so nodeB's label pointed at a
        # missing key and nodeB kept its previous capacity forever.
        other = "version: v1\n# nodeB\n"
        fake, _ = self.apply(
            [_Record("gpu-nodeA-0", "timeslice", 4)],
            cm_data={DEFAULT_KEY: "version: v1\n", f"{MANAGED_PREFIX}nodeB": other},
        )
        self.assertEqual(other, fake.cm.data.get(f"{MANAGED_PREFIX}nodeB"))
        self.assertIn(f"{MANAGED_PREFIX}nodeA", fake.cm.data)

    def test_unrelated_keys_survive_a_save(self):
        # Pre-fix clusters carry the original deploy key 'config'; it must not be dropped.
        fake, _ = self.apply([_Record("gpu-nodeA-0", "timeslice", 2)],
                             cm_data={"config": "version: v1\n"})
        self.assertIn("config", fake.cm.data)

    def test_configmap_is_patched_not_replaced(self):
        fake, _ = self.apply([_Record("gpu-nodeA-0", "timeslice", 4)],
                             cm_data={DEFAULT_KEY: "version: v1\n"})
        self.assertIn("patch_cm", fake.calls)
        self.assertNotIn("replace_cm", fake.calls)

    def test_profile_is_written_before_the_node_is_labelled(self):
        # Labelling first would briefly point the node at a key that does not exist yet.
        fake, _ = self.apply([_Record("gpu-nodeA-0", "timeslice", 4)],
                             cm_data={DEFAULT_KEY: "version: v1\n"})
        self.assertLess(fake.calls.index("patch_cm"), fake.calls.index("patch_node"))

    def test_absent_configmap_is_created_with_a_default_profile(self):
        fake, _ = self.apply([_Record("gpu-nodeC-0", "timeslice", 2, node="nodeC")],
                             node="nodeC", cm_missing=True)
        self.assertIn("create_cm", fake.calls)
        self.assertEqual("version: v1\n", fake.cm.data[DEFAULT_KEY])
        self.assertIn(f"{MANAGED_PREFIX}nodeC", fake.cm.data)

    def test_node_is_labelled_at_its_managed_profile(self):
        fake, _ = self.apply([_Record("gpu-nodeA-0", "timeslice", 4)],
                             cm_data={DEFAULT_KEY: "version: v1\n"})
        self.assertEqual(f"{MANAGED_PREFIX}nodeA", fake.node_labels["nodeA"][CONFIG_LABEL])

    def test_reverting_to_full_points_the_node_back_at_the_default(self):
        # The bug: mode="full" recorded replicas=1/applied=True but touched neither the
        # ConfigMap nor the node label, so sharing could not be switched off via the API.
        fake, result = self.apply([_Record("gpu-nodeA-0", "full", 1)],
                                  cm_data={DEFAULT_KEY: "version: v1\n"})
        self.assertEqual(DEFAULT_KEY, fake.node_labels["nodeA"][CONFIG_LABEL])
        self.assertEqual([], result["shared_devices"])

    def test_no_profile_ever_requests_a_single_replica(self):
        # The device plugin rejects replicas < 2 ("number of replicas must be >= 2"); a
        # full card is expressed by omitting the sharing section entirely.
        fake, _ = self.apply([_Record("gpu-nodeA-0", "timeslice", 1)],
                             cm_data={DEFAULT_KEY: "version: v1\n"})
        for key, profile in fake.cm.data.items():
            self.assertNotIn("replicas: 1\n", profile, f"profile {key} asks for 1 replica")

    def test_mixed_mechanisms_on_one_node_are_rejected(self):
        # The device plugin takes ONE sharing mechanism per node, so this has to surface as
        # a conflict instead of one mode silently winning.
        with self.assertRaises(FakeHTTPException) as caught:
            self.apply(
                [_Record("gpu-nodeA-0", "timeslice", 4), _Record("gpu-nodeA-1", "mps", 2)],
                cm_data={DEFAULT_KEY: "version: v1\n"},
            )
        self.assertEqual(409, caught.exception.status_code)

    def test_an_unresolvable_device_index_is_not_silently_shared(self):
        # Without an index the profile would have to fall back to "all devices", splitting
        # cards nobody asked about; the code must refuse instead.
        with self.assertRaises(RuntimeError):
            self.apply([_Record("gpu-unknown", "timeslice", 4)], index_map={},
                       cm_data={DEFAULT_KEY: "version: v1\n"})


@unittest.skipUnless(MAIN.exists(), "api/main.py is not present in this focused-test copy")
class SharingProfileShapeTests(unittest.TestCase):
    """The generated profile must match the device plugin's schema."""

    def profile(self, records, node="nodeA", index_map=None):
        fake = FakeCoreV1Api(cm_data={DEFAULT_KEY: "version: v1\n"})
        if index_map is None:
            index_map = {r.gpu_uuid: i for i, r in enumerate(records)}
        ns = load_sharing_functions(fake, index_map)
        ns["apply_device_sharing"](node, FakeDB(records))
        return fake.cm.data[f"{MANAGED_PREFIX}{node}"]

    def test_timeslice_writes_a_time_slicing_section(self):
        profile = self.profile([_Record("gpu-nodeA-0", "timeslice", 4)])
        self.assertIn("timeSlicing:", profile)
        self.assertNotIn("mps:", profile)

    def test_mps_writes_an_mps_section_not_time_slicing(self):
        # The bug: mode="mps" wrote a timeSlicing block, so the API reported MPS while the
        # node was plainly time-sliced.
        profile = self.profile([_Record("gpu-nodeB-0", "mps", 3, node="nodeB")], node="nodeB")
        self.assertIn("mps:", profile)
        self.assertNotIn("timeSlicing", profile)

    def test_device_indices_are_quoted_strings(self):
        # With `devices: [2]` the plugin does not recognise the list and quietly falls back
        # to `devices: "all"`, splitting every card on the node.
        profile = self.profile([_Record("gpu-nodeA-2", "timeslice", 4)], index_map={"gpu-nodeA-2": 2})
        self.assertIn('devices: ["2"]', profile)

    def test_only_the_recorded_cards_are_shared(self):
        # Per-card sharing: a node with three cards and one split record must name exactly
        # that one device, otherwise the other two lose their full-card capacity.
        yaml = _require_yaml()
        profile = self.profile([_Record("gpu-nodeA-1", "timeslice", 4)], index_map={"gpu-nodeA-1": 1})
        resources = yaml.safe_load(profile)["sharing"]["timeSlicing"]["resources"]
        self.assertEqual([{"name": "nvidia.com/gpu", "devices": ["1"], "replicas": 4}], resources)

    def test_every_recorded_card_keeps_its_own_replica_count(self):
        # One profile covers all of the node's split cards; splitting a second card must not
        # drop the first one's entry.
        yaml = _require_yaml()
        profile = self.profile(
            [_Record("gpu-nodeA-0", "timeslice", 2), _Record("gpu-nodeA-1", "timeslice", 4)],
            index_map={"gpu-nodeA-0": 0, "gpu-nodeA-1": 1},
        )
        resources = yaml.safe_load(profile)["sharing"]["timeSlicing"]["resources"]
        self.assertEqual([("0", 2), ("1", 4)],
                         [(r["devices"][0], r["replicas"]) for r in resources])

    def test_profiles_parse_to_the_expected_structure(self):
        yaml = _require_yaml()
        for mode, section, replicas in (("timeslice", "timeSlicing", 4), ("mps", "mps", 2)):
            with self.subTest(mode=mode):
                profile = self.profile([_Record("gpu-nodeA-0", mode, replicas)],
                                       index_map={"gpu-nodeA-0": 0})
                doc = yaml.safe_load(profile)
                self.assertEqual("v1", doc["version"])
                resource = doc["sharing"][section]["resources"][0]
                self.assertEqual("nvidia.com/gpu", resource["name"])
                self.assertEqual(replicas, resource["replicas"])

    def test_profiles_do_not_use_fields_the_plugin_ignores(self):
        # ReplicatedResource has only name/rename/devices/replicas. 'memoryLimit' looked
        # like a per-share VRAM cap in the old committed config but was never a real field.
        # 'rename' parses but is stripped at plugin start-up by
        # DisableResourceNamingInConfig(), so emitting it would be a lie about per-card
        # partitioning.
        for mode in ("timeslice", "mps"):
            profile = self.profile([_Record("gpu-nodeA-0", mode, 4)], index_map={"gpu-nodeA-0": 0})
            for field in ("memoryLimit", "rename"):
                self.assertNotIn(field, profile, f"{mode} profile emits {field}")


@unittest.skipUnless(TS_CONFIG.exists(), "time-slicing-config.yaml is not present")
class CommittedDefaultTests(unittest.TestCase):
    """The committed deploy default must not silently time-slice every GPU node."""

    def setUp(self):
        self.doc = _require_yaml().safe_load(TS_CONFIG.read_text(encoding="utf-8"))
        self.data = self.doc["data"]

    def test_default_profile_key_matches_the_api(self):
        self.assertIn(DEFAULT_KEY, self.data)

    def test_deploy_default_does_not_enable_sharing(self):
        # Was 'replicas: 4' for nvidia.com/gpu, which is why every card reported 4 shares
        # nobody had asked for. Sharing is now opt-in per card through the API.
        profile = _require_yaml().safe_load(self.data[DEFAULT_KEY])
        self.assertEqual("v1", profile["version"])
        self.assertNotIn("sharing", profile)

    def test_no_active_profile_uses_the_ignored_memory_limit_field(self):
        for key, value in self.data.items():
            self.assertNotIn("memoryLimit", value, f"profile {key} sets a non-existent field")


@unittest.skipUnless(DEPLOY.exists(), "deploy-all.sh is not present in this focused-test copy")
class ClusterPolicyAgreementTests(unittest.TestCase):
    def test_cluster_policy_names_the_same_default_profile(self):
        # The deployment and ClusterPolicy must name the same default profile so unlabeled
        # nodes always resolve to a defined device-plugin configuration.
        script = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("devicePlugin.config.name=time-slicing-config", script)
        self.assertIn(f"devicePlugin.config.default={DEFAULT_KEY}", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
