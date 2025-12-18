from services import catalog


def test_find_relevant_pages_fallbacks_to_unscoped(monkeypatch):
    calls = []
    sample_row = {
        "page_number": 1,
        "text_content": "acero inoxidable",
        "keywords": "acero,inoxidable",
        "image_filename": "img.png",
        "pdf_filename": "catalogo.pdf",
        "tenant_key": None,
    }

    def fake_search(query, limit, *, tenant_key=None, fallback_to_default=False):
        calls.append((tenant_key, fallback_to_default))
        if tenant_key == "acme":
            return []
        if tenant_key is None:
            return [sample_row]
        return []

    monkeypatch.setattr(catalog.tenants, "get_active_tenant_key", lambda include_default=False: "acme")
    monkeypatch.setattr(catalog, "search_catalog_pages", fake_search)

    pages = catalog.find_relevant_pages("acero", limit=1)

    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert calls == [("acme", False), (None, True)]


def test_find_relevant_pages_prefers_active_tenant(monkeypatch):
    calls = []
    sample_row = {
        "page_number": 2,
        "text_content": "catalogo de varillas",
        "keywords": "varillas,acero",
        "image_filename": "page2.png",
        "pdf_filename": "catalogo.pdf",
        "tenant_key": "acme",
    }

    def fake_search(query, limit, *, tenant_key=None, fallback_to_default=False):
        calls.append((tenant_key, fallback_to_default))
        if tenant_key == "acme":
            return [sample_row]
        return []

    monkeypatch.setattr(catalog.tenants, "get_active_tenant_key", lambda include_default=False: "acme")
    monkeypatch.setattr(catalog, "search_catalog_pages", fake_search)

    pages = catalog.find_relevant_pages("varillas", limit=2)

    assert len(pages) == 1
    assert pages[0].page_number == 2
    assert calls == [("acme", False)]
