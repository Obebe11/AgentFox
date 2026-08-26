"""4.3 Покрытие всех 12 направлений — матрица направление → задачи бенчмарка.

Проверяет что каждый из 12 вертикалей антидетекта (2026 интернет-исследование)
покрыт хотя бы одной задачей,
что 0 задач без направления, и каждый live имеет SKIPPED обоснование.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tools/benchmark/dataset.json"

# 12 направлений антидетекта (интернет-исследование 2026-08-25)
# Ключ — условное имя направления, значение — список ID задач которые его покрывают
DIRECTIONS = {
    # 1 Соцсети/SMM (TikTok/IG/X/Reddit)
    "social_smm": [
        "t11_xcom_search",
        "t12_xcom_profile_crawl",
        "t13_instagram_login_attempt",
        "t58_flow_x_research",
        "t33_scraping_instagram",
        "t34_scraping_tiktok",
        "t26_autoreg_xcom",
        "t27_autoreg_instagram",
        "t51_flow_autoreg",
    ],
    # 2 Арбитраж/реклама (FB/Google/TikTok Ads, multi-BM)
    "arbitrage_ads": [
        "t13_instagram_login_attempt",
        "t20_multi_profile_isolation",
        "t56_flow_incident_response",
    ],
    # 3 Аффилиат/CPA/арбитраж трафика
    "affiliate_cpa": [
        "t15_ozon_search",
        "t16_wildberries_cards",
        "t53_flow_ecom_compare",
        "t32_scraping_x_mail_cycle",
    ],
    # 4 E-commerce (Amazon/eBay/Etsy/Ozon/WB/Shopify)
    "ecommerce": [
        "t14_amazon_product",
        "t15_ozon_search",
        "t16_wildberries_cards",
        "t53_flow_ecom_compare",
        "t21_cookie_farming",
    ],
    # 5 Крипта/airdrop/multi-wallet
    "crypto": [
        "t18_crypto_airdrop_farming",
        "t57_flow_bulk_farm",
    ],
    # 6 Веб-скрапинг под антиботом (CF/DataDome/Kasada)
    "scraping_antibot": [
        "t08_cf_enterprise_bot_management",
        "t10_yandex_search",
        "t25_behavior_authenticity",
        "t06_cf_free_js_challenge",
        "t07_cf_business_turnstile",
        "t19_ticketing",
    ],
    # 7 SEO/SERP мониторинг
    "seo_serp": [
        "t10_yandex_search",
        "t58_flow_x_research",
        "t09_google_search",
    ],
    # 8 Ad verification / ресерч конкурентов
    "ad_verification": [
        "t58_flow_x_research",
        "t04_webrtc_leak",
    ],
    # 9 Бонус-хантинг/беттинг (вилки, PARI)
    "betting": [
        "t17_betting_pari",
        "t55_flow_proxy_ops",
    ],
    # 10 Тикетинг/sneaker-дропы
    "ticketing": [
        "t19_ticketing",
    ],
    # 11 QA/комплаенс/тестирование
    "qa_compliance": [
        "t01_fingerprint_bot_sannysoft",
        "t02_fingerprint_creepjs",
        "t03_fingerprint_pixelscan",
        "t04_webrtc_leak",
        "t22_warmup_gates",
        "t23_scheduler_jitter",
        "t24_proxy_rotation",
        "t54_flow_warmup_to_work",
        "t30_mail_imap_otp",
        "t31_mail_confirm_link",
    ],
    # 12 Приватность/OSINT/журналистика
    "privacy_osint": [
        "t20_multi_profile_isolation",
        "t50_flow_profile_lifecycle",
        "t24_proxy_rotation",
        "t28_autoreg_mailru",
    ],
}

# Для проверки полноты — все ID из датасета должны быть покрыты хотя бы одним направлением
ALL_IDS_IN_DIRECTIONS = {tid for lst in DIRECTIONS.values() for tid in lst}


def test_coverage_12_directions_all_present():
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    ids = {t["id"] for t in dataset}
    # 40 задач
    assert len(dataset) == 40, f"expected 40 tasks, got {len(dataset)}"
    # каждое из 12 направлений имеет хотя бы одну задачу в датасете
    for direction, task_ids in DIRECTIONS.items():
        present = [tid for tid in task_ids if tid in ids]
        assert present, f"direction {direction} has no task in dataset (expected one of {task_ids})"
    # ровно 12 направлений
    assert len(DIRECTIONS) == 12


def test_coverage_no_task_without_direction():
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    ids = {t["id"] for t in dataset}
    # каждый task должен быть в хотя бы одном направлении
    uncovered = ids - ALL_IDS_IN_DIRECTIONS
    assert not uncovered, f"tasks without direction: {uncovered} — добавь их в DIRECTIONS (TODO 4.3)"
    # проверка что категории не пустые — каждая задача имеет category
    for t in dataset:
        assert t.get("category"), f"task {t['id']} missing category"


def test_coverage_live_have_skipped_justification():
    from tools.benchmark.run import LIVE_ONLY_TASKS, run_dataset

    report = run_dataset()
    statuses = {r["id"]: r for r in report["results"]}
    for tid in LIVE_ONLY_TASKS:
        r = statuses.get(tid)
        assert r is not None, f"live task {tid} missing in report"
        assert r["status"] == "SKIPPED", f"live task {tid} should be SKIPPED offline, got {r['status']}"
        assert "authorized live target" in r["detail"] or "live" in r["detail"].lower(), f"live task {tid} missing SKIPPED justification: {r['detail']}"
    # offline tasks не должны быть SKIPPED
    for r in report["results"]:
        if r["id"] not in LIVE_ONLY_TASKS:
            assert r["status"] != "SKIPPED" or r["mode"] == "live", f"offline task {r['id']} unexpectedly SKIPPED"


def test_coverage_distribution_by_direction():
    """python -m tools.benchmark.run показывает 40 задач, 12 направлений, 0 без направления"""
    from tools.benchmark.run import run_dataset

    report = run_dataset()
    assert report["total"] == 40
    assert report["evaluated"] == 28  # offline
    assert report["skipped"] == 12  # live
    # проверяем что все 12 направлений имеют PASS или SKIPPED (не FAIL)
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    id_to_mode = {t["id"]: t["mode"] for t in dataset}
    for direction, tids in DIRECTIONS.items():
        # хотя бы один offline PASS или live SKIPPED
        has = any(
            (id_to_mode.get(tid) == "offline" and any(r["id"] == tid and r["status"] == "PASS" for r in report["results"]))
            or (id_to_mode.get(tid) == "live" and any(r["id"] == tid and r["status"] == "SKIPPED" for r in report["results"]))
            for tid in tids
        )
        assert has, f"direction {direction} has no passing offline nor skipped live task"

def test_directions_map_to_real_benchmark_ids():
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    id_to_task = {t["id"]: t for t in dataset}
    for direction, tids in DIRECTIONS.items():
        for tid in tids:
            assert tid in id_to_task, f"direction {direction} references unknown task {tid}"
