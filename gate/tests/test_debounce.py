from app.monitor.debounce import Debouncer


def test_single_blip_does_not_flip():
    d = Debouncer(2)
    assert d.observe("a", True) is None
    assert d.observe("a", False) is None
    assert d.state("a") is False


def test_two_consecutive_bad_flip_down_once():
    d = Debouncer(2)
    assert d.observe("a", True) is None
    assert d.observe("a", True) == "down"
    assert d.observe("a", True) is None
    assert d.state("a") is True


def test_recovery_needs_two_good():
    d = Debouncer(2)
    d.observe("a", True); d.observe("a", True)
    assert d.observe("a", False) is None
    assert d.state("a") is True
    assert d.observe("a", False) == "up"
    assert d.state("a") is False


def test_interleaved_blip_resets_the_count():
    d = Debouncer(2)
    d.observe("a", True); d.observe("a", False); d.observe("a", True)
    assert d.state("a") is False
    assert d.observe("a", True) == "down"


def test_keys_are_independent():
    d = Debouncer(2)
    d.observe("a", True); d.observe("b", True)
    assert d.observe("a", True) == "down"
    assert d.state("b") is False


def test_restore_sets_confirmed_state_without_event():
    d = Debouncer(2)
    d.restore("a", True)
    assert d.state("a") is True
    assert d.observe("a", True) is None   # 已经是坏，不再重复报 down
    assert d.observe("a", False) is None
    assert d.observe("a", False) == "up"


def test_threshold_one_flips_immediately():
    d = Debouncer(1)
    assert d.observe("a", True) == "down"
