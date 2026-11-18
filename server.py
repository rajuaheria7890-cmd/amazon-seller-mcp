import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

INDIA = "A21TJRUUN4KGV"
IST = ZoneInfo("Asia/Kolkata")
BASE = "https://sellingpartnerapi-eu.amazon.com"
OWNER = os.environ["AUTH0_USER_ID"]


class OwnerVerifier(JWTVerifier):
    async def verify_token(self, token):
        verified = await super().verify_token(token)
        if verified and verified.claims.get("sub") == OWNER:
            return verified
        return None


issuer = os.environ["AUTH0_ISSUER"].rstrip("/") + "/"
mcp = FastMCP("Amazon Seller India", auth=RemoteAuthProvider(
    token_verifier=OwnerVerifier(
        jwks_uri=issuer + ".well-known/jwks.json",
        issuer=issuer, audience=os.environ["AUTH0_AUDIENCE"], algorithm="RS256",
    ),
    authorization_servers=[AnyHttpUrl(issuer)],
    base_url=os.environ["PUBLIC_URL"].rstrip("/"),
))


async def amazon(path, params=None):
    async with httpx.AsyncClient(timeout=25) as client:
        login = await client.post("https://api.amazon.com/auth/o2/token", data={
            "grant_type": "refresh_token",
            "client_id": os.environ["LWA_CLIENT_ID"],
            "client_secret": os.environ["LWA_CLIENT_SECRET"],
            "refresh_token": os.environ["REFRESH_TOKEN"],
        })
        if login.is_error:
            raise ValueError(f"Amazon LWA token exchange failed (HTTP {login.status_code}).")
        response = await client.get(BASE + path, params=params, headers={
            "x-amz-access-token": login.json()["access_token"],
            "User-Agent": "PersonalSellerMCP/1.0 (Language=Python)",
        })
        if response.is_error:
            request_id = response.headers.get("x-amzn-RequestId", "unavailable")
            raise ValueError(f"SP-API returned HTTP {response.status_code}, request ID {request_id}.")
        return response.json()["payload"]


@mcp.tool(annotations={"readOnlyHint": True})
async def seller_marketplaces() -> list:
    """List seller marketplace participations in the Europe region, including India."""
    return await amazon("/sellers/v1/marketplaceParticipations")


@mcp.tool(annotations={"readOnlyHint": True})
async def india_daily_sales(day: date) -> list:
    """Read Amazon.in order metrics for one completed India calendar day (YYYY-MM-DD)."""
    if day >= datetime.now(IST).date():
        raise ValueError("Choose a completed calendar day in India.")
    start = datetime.combine(day, time.min, IST)
    end = start + timedelta(days=1)
    return await amazon("/sales/v1/orderMetrics", {
        "marketplaceIds": INDIA,
        "interval": f"{start.isoformat()}--{end.isoformat()}",
        "granularity": "Total",
    })


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0",
            port=int(os.getenv("PORT", "8000")), path="/mcp", stateless_http=True)
