"""Amazon.in listing tools and a human-only approval page."""

import asyncio
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from html import escape
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, quote

from starlette.requests import Request
from starlette.responses import HTMLResponse

EDITABLE = {
    "item_name", "bullet_point", "product_description", "generic_keyword",
    "brand", "manufacturer", "model_name", "model_number", "part_number",
    "color", "size", "material", "capacity", "item_dimensions", "item_weight",
    "number_of_items", "item_package_quantity", "style", "pattern", "finish_type",
    "care_instructions", "included_components", "country_of_origin",
    "parentage_level", "child_parent_sku_relationship", "variation_theme",
    "main_product_image_locator",
    *(f"other_product_image_locator_{slot}" for slot in range(1, 9)),
}
FORBIDDEN_KEY = re.compile(r"price|pricing|currency|purchasable_offer|discount", re.I)
TTL = 15 * 60
MAX_PENDING = 32
MAX_JSON_CHARS = 16000
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@dataclass(frozen=True)
class Proposal:
    sku: str
    path: str
    before: str
    payload: str
    expires: float


def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)


def validate_attributes(attributes: dict, marketplace: str) -> None:
    if not attributes or attributes.keys() - EDITABLE:
        raise ValueError("Use supported non-price listing attributes only.")
    if len(canonical(attributes)) > MAX_JSON_CHARS:
        raise ValueError("Listing changes exceed the size limit.")

    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if FORBIDDEN_KEY.search(key):
                    raise ValueError("Price and offer fields are blocked.")
                if key == "marketplace_id" and child != marketplace:
                    raise ValueError("Only the Amazon.in marketplace is supported.")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    if any(not isinstance(value, list) or not value or
           any(not isinstance(entry, dict) for entry in value)
           for value in attributes.values()):
        raise ValueError("Supply a non-empty array of objects for each attribute.")
    check(attributes)


def register_listing_tools(mcp, amazon, marketplace: str) -> None:
    pending: dict[str, Proposal] = {}
    write_lock = asyncio.Lock()
    params = {"marketplaceIds": marketplace, "issueLocale": "en_IN"}
    listing_data = "summaries,attributes,issues,fulfillmentAvailability"

    def root() -> str:
        seller = os.getenv("AMAZON_SELLER_ID", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]+", seller):
            raise ValueError("Configure AMAZON_SELLER_ID in the service environment.")
        return "/listings/2021-08-01/items/" + seller

    def item_path(sku: str) -> str:
        if not sku.strip() or sku in {".", ".."} or len(sku) > 40:
            raise ValueError("Provide a valid Seller SKU.")
        return root() + "/" + quote(sku, safe="")

    async def read_item(path: str) -> dict:
        return await amazon(path, {**params, "includedData": listing_data}, unwrap=False)

    def product_type(item: dict) -> str:
        for summary in item.get("summaries", []):
            if summary.get("marketplaceId") == marketplace and summary.get("productType"):
                return summary["productType"]
        raise ValueError("Amazon did not return an India product type for this SKU.")

    def current_values(item: dict, patches: list[dict]) -> str:
        attrs = item.get("attributes", {})
        return canonical({
            "productType": product_type(item),
            "attributes": {p["path"].split("/")[-1]: attrs.get(p["path"].split("/")[-1])
                           for p in patches},
        })

    def approval_key() -> str:
        key = os.getenv("LISTING_APPROVAL_KEY", "")
        if not 32 <= len(key) <= 256 or not key.isascii():
            raise ValueError("Set a unique ASCII LISTING_APPROVAL_KEY of 32 to 256 characters.")
        return key

    async def stage(sku: str, path: str, item: dict, patches: list[dict]) -> dict:
        approval_key()
        payload = {"productType": product_type(item), "patches": patches}
        result = await amazon(path, {**params, "mode": "VALIDATION_PREVIEW"},
                              method="PATCH", body=payload, unwrap=False)
        if result.get("status") != "VALID":
            return {"state": "validation_failed", "amazon": result}
        now = monotonic()
        for proposal_id in [key for key, entry in pending.items() if entry.expires <= now]:
            del pending[proposal_id]
        if len(pending) >= MAX_PENDING:
            raise ValueError("Too many pending proposals. Let one expire or approve one first.")
        token = secrets.token_urlsafe(24)
        before = current_values(item, patches)
        pending[token] = Proposal(sku, path, before, canonical(payload), now + TTL)
        return {
            "state": "awaiting_owner_approval",
            "sku": sku,
            "before": json.loads(before),
            "proposed": payload,
            "approval_url": os.environ["PUBLIC_URL"].rstrip("/") + "/listing-approval/" + token,
            "expires_in_minutes": TTL // 60,
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def india_listing_requirements(product_type_name: str) -> dict:
        """Return Amazon.in's seller-specific schema link and requirements for a product type."""
        if not re.fullmatch(r"[A-Z0-9_]+", product_type_name):
            raise ValueError("Provide a valid Amazon product type.")
        return await amazon("/definitions/2020-09-01/productTypes/" + product_type_name, {
            "sellerId": root().rsplit("/", 1)[-1], "marketplaceIds": marketplace,
            "requirements": "LISTING_PRODUCT_ONLY", "requirementsEnforced": "ENFORCED",
            "locale": "en_IN",
        }, unwrap=False)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def india_search_listings(page_token: str | None = None) -> dict:
        """List your Amazon.in listings and their issues. Use the returned next token to continue."""
        query = {**params, "includedData": "summaries,issues", "pageSize": 20}
        if page_token:
            query["pageToken"] = page_token
        return await amazon(root(), query, unwrap=False)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def india_get_listing(sku: str) -> dict:
        """Fetch Amazon.in listing status, submitted attributes, issues and stock for a seller SKU."""
        return await read_item(item_path(sku))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def india_fba_inventory(sku: str | None = None, next_token: str | None = None) -> dict:
        """Read FBA inventory in India. Optionally filter by seller SKU."""
        query = {"granularityType": "Marketplace", "granularityId": marketplace,
                 "marketplaceIds": marketplace, "details": "true"}
        if sku:
            query["sellerSkus"] = sku
        if next_token:
            query["nextToken"] = next_token
        return await amazon("/fba/inventory/v1/summaries", query, unwrap=False)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def india_validate_new_listing(sku: str, product_type_name: str,
                                         attributes: dict[str, list[dict[str, Any]]]) -> dict:
        """Ask Amazon to validate a new product draft without publishing it."""
        validate_attributes(attributes, marketplace)
        return await amazon(item_path(sku), {**params, "mode": "VALIDATION_PREVIEW"},
                            method="PUT", unwrap=False, body={
                                "productType": product_type_name,
                                "requirements": "LISTING_PRODUCT_ONLY",
                                "attributes": attributes,
                            })

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def india_propose_listing_edits(sku: str, attributes: dict[str, list[dict[str, Any]]]) -> dict:
        """Prepare a non-price listing edit. Only the seller can apply it on the approval page."""
        validate_attributes(attributes, marketplace)
        path = item_path(sku)
        item = await read_item(path)
        patches = [{"op": "replace", "path": "/attributes/" + name, "value": value}
                   for name, value in sorted(attributes.items())]
        return await stage(sku, path, item, patches)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def india_propose_stock_quantity(sku: str, quantity: int) -> dict:
        """Prepare a default seller-fulfilled stock update for the seller to approve."""
        if quantity < 0 or isinstance(quantity, bool):
            raise ValueError("Quantity must be a non-negative integer.")
        path = item_path(sku)
        item = await read_item(path)
        availability = item.get("attributes", {}).get("fulfillment_availability", [])
        if (len(availability) != 1 or availability[0].get("fulfillment_channel_code") != "DEFAULT"
                or availability[0].get("marketplace_id", marketplace) != marketplace
                or "is_inventory_available" in availability[0]):
            raise ValueError("This SKU needs a supported single default seller-fulfilled channel.")
        updated = {**availability[0], "quantity": quantity}
        return await stage(sku, path, item, [{
            "op": "replace", "path": "/attributes/fulfillment_availability", "value": [updated],
        }])

    def page(title: str, content: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)}</title><main><h1>{escape(title)}</h1>{content}</main></html>',
            status_code=status, headers=HEADERS,
        )

    @mcp.custom_route("/listing-approval/{token}", methods=["GET", "POST"], include_in_schema=False)
    async def approve_listing(request: Request) -> HTMLResponse:
        token = request.path_params["token"]
        proposal = pending.get(token)
        if proposal is None or proposal.expires <= monotonic():
            pending.pop(token, None)
            return page("Proposal unavailable", "<p>It may have expired or already been used.</p>", 404)
        payload = json.loads(proposal.payload)
        if request.method == "GET":
            data = {"marketplace": "Amazon.in", "sku": proposal.sku,
                    "current": json.loads(proposal.before), "proposed": payload}
            details = escape(json.dumps(data, indent=2, ensure_ascii=False))
            return page("Review Amazon.in listing change", f"""
                <pre>{details}</pre>
                <form method="post">
                    <label>Listing approval key
                        <input name="approval_key" type="password" required maxlength="256">
                    </label>
                    <button type="submit">Approve and send to Amazon</button>
                </form>
            """)

        public_origin = os.environ["PUBLIC_URL"].rstrip("/")
        origin = request.headers.get("origin")
        if origin != public_origin:
            return page("Approval blocked", "<p>Open the approval link directly in your browser.</p>", 403)
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 2048:
                return page("Invalid approval form", "", 400)
        try:
            fields = parse_qs(chunks.decode("ascii", errors="strict"), max_num_fields=4)
        except ValueError:
            return page("Invalid approval form", "", 400)
        supplied = fields.get("approval_key", [""])[0]
        if not hmac.compare_digest(supplied.encode("utf-8"), approval_key().encode("ascii")):
            return page("Approval key did not match", "<p>Reopen the link to try again.</p>", 403)

        async with write_lock:
            if pending.pop(token, None) is not proposal or proposal.expires <= monotonic():
                return page("Proposal unavailable", "<p>Generate a fresh proposal.</p>", 409)
            try:
                item = await read_item(proposal.path)
                if current_values(item, payload["patches"]) != proposal.before:
                    return page("Listing changed", "<p>Generate a fresh proposal and review it again.</p>", 409)
            except Exception:
                return page("Listing could not be checked", "<p>No change was submitted.</p>", 502)
            try:
                result = await amazon(proposal.path, params, method="PATCH", body=payload, unwrap=False)
            except Exception:
                return page("Submission status could not be confirmed",
                            "<p>Check the listing in Amazon before requesting another change.</p>", 502)
        amazon_status = result.get("status", "UNKNOWN")
        details = escape(json.dumps(result, indent=2, ensure_ascii=False))
        return page(f"Amazon submission status: {amazon_status}", f"<pre>{details}</pre>")
