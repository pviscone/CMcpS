"""Fixture-driven contract tests for the GLANCE/CMSFence adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cmcps.adapters import glance
from cmcps.auth.service import AuthService
from cmcps.auth.tokens import ServiceTokenRecord, ServiceTokenStore
from cmcps.config.settings import Settings
from cmcps.contracts.errors import InvalidInputError, UpstreamError
from cmcps.net.client import SafeHttpClient

Handler = Callable[[httpx.Request], httpx.Response]

ALCM_TOKEN = "unit-test-alcm-token"  # noqa: S105 - mock credential, not a real secret
MEM_TOKEN = "unit-test-mem-token"  # noqa: S105 - mock credential, not a real secret

ANALYSIS_PAYLOAD = {
    "analysis": [
        {
            "id": 19821,
            "customIdentifier": "TOP-25-018",
            "title": "Single top t-channel at 13.6 TeV",
            "paperIdentifier": "TOP-25-018",
            "workingGroups": [{"id": 21, "name": "TOP", "fullName": "TOP", "parent": None}],
            "currentStage": {"id": 213, "name": "Ready for CWR"},
            "proponents": [{"id": 17960, "name": "Jane Doe"}],
            "hashtags": [],
            "registrationDate": "2026-10-23",
            "gitlabpasrepo": "https://gitlab.cern.ch/tdr/notes/TOP-25-018",
            "pasCDS": "https://cds.cern.ch/record/2972401",
            "pasReadyforPUB": "true",
            "gitlabpaperrepo": "https://gitlab.cern.ch/tdr/papers/TOP-25-018",
        },
        {
            "id": 19788,
            "customIdentifier": "HIN-25-017",
            "title": "Jet quenching with Z+jet events",
            "paperIdentifier": "HIN-25-017",
            "workingGroups": [{"id": 10, "name": "HIN", "fullName": "HIN", "parent": None}],
            "currentStage": {"id": 208, "name": "CWR"},
            "proponents": [{"id": 17125, "name": "John Smith"}],
            "hashtags": [],
            "registrationDate": "2026-10-22",
            "gitlabpasrepo": "https://gitlab.cern.ch/tdr/notes/HIN-25-017",
            "pasCDS": "https://cds.cern.ch/record/2966390",
            "pasReadyforPUB": "true",
            "gitlabpaperrepo": "https://gitlab.cern.ch/tdr/papers/HIN-25-017",
        },
    ]
}

PUBLISHED_PAYLOAD = {
    "analyses": [
        {
            "id": 19884,
            "title": "Search for tau-enriched SVJs (Run-2)",
            "paperIdentifier": "EXO-26-005",
            "arxivNumber": None,
            "workingGroups": [{"id": 6, "name": "EXO", "fullName": "EXO", "parent": None}],
            "hashtags": [],
            "submittedOn": "2026-04-01",
            "finalPaperDraftCdsLink": None,
            "journalReference": None,
            "journalUrl": None,
        },
        {
            "id": 19791,
            "title": "Quantum state of ttbar",
            "paperIdentifier": "TOP-25-016",
            "arxivNumber": "2512.17557",
            "workingGroups": [{"id": 21, "name": "TOP", "fullName": "TOP", "parent": None}],
            "hashtags": [],
            "submittedOn": "2026-03-13",
            "finalPaperDraftCdsLink": "https://cds.cern.ch/record/2951662",
            "journalReference": "PRD-RC",
            "journalUrl": "https://doi.org/10.1103/hkgw-dztk",
        },
    ]
}

NOTES_PAYLOAD = {
    "notes": [
        {
            "id": 20873,
            "type": {"id": 2, "name": "Conference Report"},
            "title": "Electroweak top-quark production at CMS",
            "registrationDate": "2026-10-22",
            "reportNumber": "CMS CR-2026/257",
            "publishedIn": "ICHEP2026",
            "gitlabRepositoryUrl": None,
            "cdsLink": None,
            "latestCIArtifactsUrl": None,
            "workingGroups": [{"id": 21, "name": "TOP", "fullName": "TOP", "parent": None}],
            "projects": [{"id": 17, "name": "Physics", "roleName": "cms-physics", "egroups": []}],
            "hashtags": [],
            "proponents": [{"id": 15868, "name": "Alberto Belvedere"}],
            "authors": [{"id": 15868, "name": "Alberto Belvedere"}],
            "authorsText": None,
            "role": None,
            "currentStage": {"id": 102, "name": "Sub-editor(s) approval"},
        },
        {
            "id": 20872,
            "type": {"id": 5, "name": "Analysis Note"},
            "title": "MaPSA production testing",
            "registrationDate": "2026-10-22",
            "reportNumber": "CMS AN-2026/256",
            "publishedIn": None,
            "gitlabRepositoryUrl": None,
            "cdsLink": None,
            "latestCIArtifactsUrl": None,
            "workingGroups": [],
            "projects": [{"id": 9, "name": "TK", "roleName": "cms-tracker", "egroups": []}],
            "hashtags": [],
            "proponents": [{"id": 19105, "name": "Leticia Braga"}],
            "authors": [{"id": 19105, "name": "Leticia Braga"}],
            "authorsText": None,
            "role": None,
            "currentStage": {"id": 102, "name": "Sub-editor(s) approval"},
        },
    ]
}

NOTE_DETAIL_PAYLOAD = {"note": {"id": 20873, "title": "Electroweak top-quark production at CMS"}}

MEMBERS_PAYLOAD = {
    "members": [
        {"id": 20118, "name": "Yagizhan DINCKURT (YILDIZ, 20118)"},
        {"id": 2669, "name": "A Thong Tien (HEPHY, 2669)"},
        {"id": 4321, "name": "Jane Doe (CERN, 4321)"},
    ]
}

MEM_SEARCH_PAYLOAD = {
    "results": [
        {
            "id": "4",
            "fullName": "Albert M. Sirunyan",
            "membershipAccessName": "DECEASED",
            "isAuthor": "No",
            "instituteName": "Yerevan Physics Institute",
        },
        {
            "id": "7",
            "fullName": "Norbert Neumeister",
            "membershipAccessName": "CMS",
            "isAuthor": "Yes",
            "instituteName": "Purdue University",
        },
    ],
    "numberOfResults": "2",
}


class Recorder:
    """Mock-transport handler that records every request."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(
    tmp_path: Path,
    recorder: Recorder,
    *,
    alcm_token: str | None = ALCM_TOKEN,
    mem_token: str | None = MEM_TOKEN,
) -> tuple[Settings, SafeHttpClient]:
    settings = Settings(
        state_dir=tmp_path,
        require_sso=False,
        browser_executable=None,
        glance_alcm_token=alcm_token,
        glance_membership_token=mem_token,
    )
    auth = AuthService(settings)
    client = SafeHttpClient(
        settings,
        auth_service=auth,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    return settings, client


async def test_alcm_user_injects_bearer_and_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {ALCM_TOKEN}"
        assert request.url.path == "/alcm/api/user"
        return httpx.Response(200, json={"user": {"username": "pviscone"}})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await glance.alcm_user(client, settings)
    assert result["user"]["username"] == "pviscone"
    assert result["app"] == "alcm"


async def test_membership_user_uses_membership_token_and_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {MEM_TOKEN}"
        assert request.url.path == "/membership/api/user"
        return httpx.Response(200, json={"user": {"roles": ["default-role"]}})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await glance.membership_user(client, settings)
    assert result["app"] == "membership"


async def test_missing_alcm_token_rejected_without_request(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder, alcm_token=None, mem_token=None)
    with pytest.raises(InvalidInputError) as excinfo:
        await glance.alcm_user(client, settings)
    assert "CMCPS_GLANCE_ALCM_TOKEN" in (excinfo.value.instructions or "")
    assert "cms-alcm-api-prod" in (excinfo.value.instructions or "")
    assert recorder.requests == []


async def test_401_maps_to_helpful_upstream(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(401, json={"message": "Unauthorized"}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(UpstreamError) as excinfo:
        await glance.list_note_types(client, settings)
    message = excinfo.value.message + (excinfo.value.instructions or "")
    assert "cms-alcm-api-prod" in message
    assert "cern_auth_sign_in" in message


async def test_captured_bearer_wins_over_env_token(tmp_path: Path) -> None:
    record = ServiceTokenRecord(
        service="glance_alcm",
        token_endpoint="https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/token",  # noqa: S106
        client_id="cms-alcm-client-prod",
        access_token="captured-alcm-value",  # noqa: S106
        refresh_token=None,
        access_expires_at=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer captured-alcm-value"
        return httpx.Response(200, json={"analysis": []})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    ServiceTokenStore(settings.service_tokens_path).save(record)
    result = await glance.list_analyses(client, settings)
    assert result.total_count == 0


async def test_captured_membership_bearer_wins_over_env_token(tmp_path: Path) -> None:
    record = ServiceTokenRecord(
        service="glance_membership",
        token_endpoint="https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/token",  # noqa: S106
        client_id="cms-membership-client-prod",
        access_token="captured-mem-value",  # noqa: S106
        refresh_token=None,
        access_expires_at=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer captured-mem-value"
        return httpx.Response(200, json={"results": [], "numberOfResults": "0"})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    ServiceTokenStore(settings.service_tokens_path).save(record)
    result = await glance.membership_search_members(client, settings, query="Doe")
    assert result.total_count == 0


async def test_list_analyses_filters_and_bounds_output(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/alcm/api/analysis"
        return httpx.Response(200, json=ANALYSIS_PAYLOAD)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await glance.list_analyses(client, settings, custom="top-25")
    assert result.total_count == 1
    item = result.items[0]
    assert item["customIdentifier"] == "TOP-25-018"
    assert "hashtags" not in item  # output restricted to known useful fields
    assert "pasReadyforPUB" not in item

    result = await glance.list_analyses(client, settings, working_group="HIN", stage="cw")
    assert result.total_count == 1
    assert result.items[0]["paperIdentifier"] == "HIN-25-017"

    result = await glance.list_analyses(client, settings, title="missing")
    assert result.total_count == 0


async def test_list_analyses_spa_field_filters(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=ANALYSIS_PAYLOAD))
    settings, client = make_client(tmp_path, recorder)
    by_code = await glance.list_analyses(client, settings, code="hin-25")
    assert [i["id"] for i in by_code.items] == [19788]
    by_contact = await glance.list_analyses(client, settings, contacts="jane doe")
    assert [i["id"] for i in by_contact.items] == [19821]
    by_window = await glance.list_analyses(
        client, settings, start_date="2026-10-23", end_date="2026-10-23"
    )
    assert [i["id"] for i in by_window.items] == [19821]
    before = await glance.list_analyses(client, settings, end_date="2026-10-22")
    assert [i["id"] for i in before.items] == [19788]


async def test_list_analyses_sorting(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=ANALYSIS_PAYLOAD))
    settings, client = make_client(tmp_path, recorder)
    by_title = await glance.list_analyses(client, settings, sort_by="title", sort_order="asc")
    assert [i["id"] for i in by_title.items] == [19788, 19821]
    by_date = await glance.list_analyses(
        client, settings, sort_by="registrationDate", sort_order="desc"
    )
    assert [i["id"] for i in by_date.items] == [19821, 19788]


async def test_list_analyses_pagination(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=ANALYSIS_PAYLOAD))
    settings, client = make_client(tmp_path, recorder)
    page1 = await glance.list_analyses(client, settings, limit=1)
    page2 = await glance.list_analyses(client, settings, limit=1, offset=1)
    assert page1.total_count == 2
    assert page1.has_more is True
    assert page1.page == 1 and page1.size == 1
    assert page2.has_more is False
    assert page2.page == 2
    assert page1.items[0]["id"] != page2.items[0]["id"]


async def test_list_analyses_rejects_bad_page_and_filter(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await glance.list_analyses(client, settings, limit=0)
    with pytest.raises(InvalidInputError):
        await glance.list_analyses(client, settings, offset=-1)
    with pytest.raises(InvalidInputError):
        await glance.list_analyses(client, settings, custom="   ")
    with pytest.raises(InvalidInputError):
        await glance.list_analyses(client, settings, start_date="23/10/2026")
    with pytest.raises(InvalidInputError):
        await glance.list_analyses(client, settings, sort_by="color")
    with pytest.raises(InvalidInputError):
        await glance.list_analyses(client, settings, sort_order="up")
    assert recorder.requests == []


async def test_list_published_analyses_filters(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=PUBLISHED_PAYLOAD))
    settings, client = make_client(tmp_path, recorder)

    result = await glance.list_published_analyses(client, settings, has_arxiv=True)
    assert result.total_count == 1
    assert result.items[0]["arxivNumber"] == "2512.17557"
    assert "hashtags" not in result.items[0]

    result = await glance.list_published_analyses(client, settings, has_arxiv=False)
    assert result.items[0]["paperIdentifier"] == "EXO-26-005"

    result = await glance.list_published_analyses(client, settings, working_group="top")
    assert result.total_count == 1


async def test_search_notes_client_side_filters(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/alcm/api/notes"
        return httpx.Response(200, json=NOTES_PAYLOAD)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)

    result = await glance.search_notes(client, settings, type_name="Analysis Note")
    assert result.total_count == 1
    assert result.items[0]["reportNumber"] == "CMS AN-2026/256"
    assert "latestCIArtifactsUrl" not in result.items[0]

    result = await glance.search_notes(client, settings, working_group="TOP", project="Physics")
    assert result.total_count == 1

    result = await glance.search_notes(client, settings, title_contains="mapsa")
    assert result.total_count == 1
    assert "~15 MB" in (result.note or "")


ANALYSIS_DETAIL_ROUTES = {
    "/alcm/api/analysis/ancode/EXO-24-010": {
        "analysis": {
            "id": 19556,
            "customId": "EXO-24-010",
            "paperIdentifier": "EXO-24-010",
            "publicResults": "Yes",
            "workingGroups": [{"id": 6, "name": "EXO", "fullName": "EXO", "parent": None}],
            "notesList": [{"id": 15225, "reportNumber": "CMS AN-2020/185"}],
            "twikiUrl": "https://twiki.cern.ch/twiki/bin/view/CMS/EXO24010",
        }
    },
    "/alcm/api/analysis/19556": {"analysis": {"id": 19556, "customId": "EXO-24-010"}},
    "/alcm/api/analysis/19556/paper": {
        "paper": {"egroup": "cms-exo-24-010", "targetJournal": {"name": "JHEP"}}
    },
    "/alcm/api/analysis/19556/review-committee": {
        "reviewCommittee": {
            "egroup": "cms-arc-exo-24-010",
            "chair": {"id": 406, "name": "Yuri Gershtein"},
            "le": [{"id": 7522, "name": "Kevin Pedro"}],
            "members": [{"id": 14007, "name": "Anne-mazarine Lyon"}],
        }
    },
    "/alcm/api/transitions/19556": {
        "transition": {"currentStage": {"name": "Ready for Submission"}, "pastEvents": []}
    },
    "/alcm/api/analysis/19556/history": {"history": {"analysisUpdates": []}},
    "/alcm/api/attachments/documents/entity/19556": {
        "documents": [
            {"id": 29410, "title": "PAS", "versions": [{"fileName": "EXO-24-010-pas-v0"}]}
        ]
    },
    "/alcm/api/attachments/gitlab-repositories/entity/19556": {
        "gitlabRepositories": [{"url": "https://gitlab.cern.ch/tdr/notes/EXO-24-010"}]
    },
    "/alcm/api/attachments/indico-contributions/entity/19556": {"indicoContributions": []},
    "/alcm/api/attachments/paths/entity/19556": {"paths": []},
    "/alcm/api/hashtags/entity/19556": {"hashtags": []},
    "/alcm/api/attachments/documents/entity/15225": {
        "documents": [
            {"id": 33000, "title": "AN-2020/185", "versions": [{"fileName": "CMS_AN_2020_185"}]}
        ]
    },
}


async def test_get_analysis_page_composite(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = ANALYSIS_DETAIL_ROUTES.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={"errors": [{"title": "Endpoint not found."}]})
        return httpx.Response(200, json=payload)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await glance.get_analysis(client, settings, analysis="EXO-24-010")
    assert result["analysis_code"] == "EXO-24-010"
    assert result["analysis_id"] == 19556
    assert result["review_committee"]["chair"]["name"] == "Yuri Gershtein"
    assert result["review_committee"]["members"][0]["name"] == "Anne-mazarine Lyon"
    assert result["paper"]["egroup"] == "cms-exo-24-010"
    assert result["workflow"]["currentStage"]["name"] == "Ready for Submission"
    assert result["attachments"]["documents"][0]["versions"][0]["fileName"] == ("EXO-24-010-pas-v0")
    assert result["attachments"]["gitlab_repositories"][0]["url"].endswith("EXO-24-010")
    assert result["links"]["pubtalk_forum"] == "https://cms-pub-talk.web.cern.ch/c/EXO/EXO-24-010"
    assert result["links"]["public_result"] == (
        "https://cms-results.web.cern.ch/cms-results/public-results/preliminary-results/EXO-24-010/"
    )
    assert result["links"]["twiki"] == "https://twiki.cern.ch/twiki/bin/view/CMS/EXO24010"
    assert result["attachments"]["related_note_documents"][15225][0]["id"] == 33000
    weak = await glance.get_analysis(client, settings, analysis="19556")
    assert weak["links"]["pubtalk_forum"] is None
    assert weak["links"]["public_result"] is None
    assert weak["attachments"]["related_note_documents"] == {}

    # Numeric id takes the id route, not the ancode route.
    result_by_id = await glance.get_analysis(client, settings, analysis="19556")
    assert result_by_id["analysis_code"] == "EXO-24-010"
    called = [httpx.URL(str(r.url)).path for r in recorder.requests]
    assert "/alcm/api/analysis/ancode/EXO-24-010" not in called[1:]


async def test_get_analysis_support_endpoint_failure_is_nonfatal(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/alcm/api/analysis/ancode/EXO-24-010":
            return httpx.Response(200, json={"analysis": {"id": 19556, "customId": "EXO-24-010"}})
        return httpx.Response(404, json={"errors": [{"title": "Endpoint not found."}]})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await glance.get_analysis(client, settings, analysis="EXO-24-010")

    assert result["analysis"]["customId"] == "EXO-24-010"
    assert "error" in result["paper"]
    assert "review-committee" in result["review_committee"]["error"]
    assert isinstance(result["attachments"]["documents"], dict)
    assert "error" in result["attachments"]["documents"]


async def test_get_analysis_rejects_bad_input_and_missing(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"analysis": None}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await glance.get_analysis(client, settings, analysis="   ")
    assert recorder.requests == []
    with pytest.raises(UpstreamError):
        await glance.get_analysis(client, settings, analysis="EXO-00-000")


async def test_get_note_url_and_payload(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/alcm/api/notes/20873"
        return httpx.Response(200, json=NOTE_DETAIL_PAYLOAD)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await glance.get_note(client, settings, note_id=20873)
    assert result["note"]["id"] == 20873


async def test_get_note_rejects_bad_id_and_empty(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={"note": None}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await glance.get_note(client, settings, note_id=0)
    assert recorder.requests == []
    with pytest.raises(UpstreamError):
        await glance.get_note(client, settings, note_id=1)


async def test_working_groups_kind_selection(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/alcm/api/working-groups":
            return httpx.Response(200, json={"workingGroups": [{"id": 2, "name": "BPH"}]})
        if request.url.path == "/alcm/api/physics-working-groups":
            return httpx.Response(
                200,
                json={"physicsWorkingGroups": [{"id": 1, "name": "B2G", "parent": None}]},
            )
        return httpx.Response(404, json={})

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)

    result = await glance.list_working_groups(client, settings, kind="physics")
    assert "physics_working_groups" in result
    assert "working_groups" not in result
    assert len(recorder.requests) == 1

    result = await glance.list_working_groups(client, settings, kind="all")
    assert result["working_groups"][0]["name"] == "BPH"
    assert result["physics_working_groups"][0]["name"] == "B2G"


async def test_alcm_members_substring_and_pagination(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json=MEMBERS_PAYLOAD))
    settings, client = make_client(tmp_path, recorder)
    result = await glance.alcm_members(client, settings, name_contains="doe")
    assert result.total_count == 1
    assert result.items[0]["name"].startswith("Jane Doe")
    result = await glance.alcm_members(client, settings, limit=2, offset=2)
    assert result.total_count == 3
    assert result.has_more is False
    assert result.size == 1


async def test_membership_search_builds_query_param(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/membership/api/members/search"
        assert request.url.params["q"] == "neumeister"
        return httpx.Response(200, json=MEM_SEARCH_PAYLOAD)

    recorder = Recorder(handler)
    settings, client = make_client(tmp_path, recorder)
    result = await glance.membership_search_members(client, settings, query="neumeister", limit=1)
    assert result.total_count == 2
    assert result.has_more is True
    assert result.items[0]["fullName"] == "Albert M. Sirunyan"


async def test_membership_search_requires_query(tmp_path: Path) -> None:
    recorder = Recorder(lambda _r: httpx.Response(200, json={}))
    settings, client = make_client(tmp_path, recorder)
    with pytest.raises(InvalidInputError):
        await glance.membership_search_members(client, settings, query=" ")
    assert recorder.requests == []
