"""Publish new DailyPropHub social posts (site/social/posts/*.json) to X and Instagram.

Run by .github/workflows/social.yml. Standard library only. A platform is skipped (not failed) when its secrets
are not set yet, so this can ship before the accounts exist.

X:          X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET   (OAuth 1.0a user context, read+write)
Instagram:  IG_TOKEN (Instagram Login long-lived token), optional IG_USER_ID (defaults to "me")
"""
import base64, hashlib, hmac, json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request

UA = {"User-Agent": "DailyPropHub-social/1.0"}


def http(method, url, data=None, headers=None, timeout=60):
    body = None
    h = dict(UA, **(headers or {}))
    if isinstance(data, dict) and h.get("Content-Type") == "application/json":
        body = json.dumps(data).encode()
    elif isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode(); h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode() or "{}"
            return r.status, json.loads(txt) if txt.strip().startswith(("{", "[")) else txt
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        return e.code, txt


def wait_live(url, minutes=15):
    """GitHub Pages deploys in parallel with this job; Instagram fetches image_url itself, so wait until it serves."""
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=20) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(20)
    return False


# ------------------------------------------------------------------ X (OAuth 1.0a)

def oauth_header(method, url, keys, params=None):
    pct = lambda s: urllib.parse.quote(str(s), safe="~")
    o = {"oauth_consumer_key": keys["X_API_KEY"], "oauth_nonce": secrets.token_hex(16), "oauth_signature_method": "HMAC-SHA1",
         "oauth_timestamp": str(int(time.time())), "oauth_token": keys["X_ACCESS_TOKEN"], "oauth_version": "1.0"}
    allp = {**o, **(params or {})}           # JSON bodies are not part of the signature
    base = "&".join([method.upper(), pct(url), pct("&".join(f"{pct(k)}={pct(v)}" for k, v in sorted(allp.items())))])
    key = f"{pct(keys['X_API_SECRET'])}&{pct(keys['X_ACCESS_SECRET'])}"
    o["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{pct(k)}="{pct(v)}"' for k, v in sorted(o.items()))


def post_x(post, img_bytes, keys):
    up = "https://api.x.com/2/media/upload"
    st, r = http("POST", up, {"media": base64.b64encode(img_bytes).decode(), "media_category": "tweet_image",
                              "media_type": "image/jpeg"},
                 {"Content-Type": "application/json", "Authorization": oauth_header("POST", up, keys)})
    if st >= 300 or not isinstance(r, dict) or "data" not in r:
        raise RuntimeError(f"X media upload failed ({st}): {str(r)[:400]}")
    media_id = r["data"]["id"]
    url = "https://api.x.com/2/tweets"
    st, r = http("POST", url, {"text": post["x_text"], "media": {"media_ids": [media_id]}},
                 {"Content-Type": "application/json", "Authorization": oauth_header("POST", url, keys)})
    if st >= 300:
        raise RuntimeError(f"X post failed ({st}): {str(r)[:400]}")
    return f"https://x.com/i/status/{r['data']['id']}"


# ------------------------------------------------------------------ Instagram (Instagram API with Instagram Login)

def post_ig(post, token, user="me"):
    g = "https://graph.instagram.com"
    st, r = http("POST", f"{g}/{user}/media", {"image_url": post["image_url"], "caption": post["ig_caption"][:2200],
                                              "alt_text": post.get("alt", "")[:1000], "access_token": token})
    if st >= 300 or not isinstance(r, dict) or "id" not in r:
        raise RuntimeError(f"Instagram container failed ({st}): {str(r)[:400]}")
    cid = r["id"]
    for _ in range(30):                                  # wait for Instagram to finish fetching the image
        st, s = http("GET", f"{g}/{cid}?fields=status_code&access_token={urllib.parse.quote(token)}")
        if isinstance(s, dict) and s.get("status_code") == "FINISHED":
            break
        if isinstance(s, dict) and s.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram could not process the image: {s}")
        time.sleep(5)
    st, r = http("POST", f"{g}/{user}/media_publish", {"creation_id": cid, "access_token": token})
    if st >= 300 or not isinstance(r, dict) or "id" not in r:
        raise RuntimeError(f"Instagram publish failed ({st}): {str(r)[:400]}")
    return f"instagram media {r['id']}"


def main(paths):
    xk = {k: os.environ.get(k, "") for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}
    have_x = all(xk.values()); ig = os.environ.get("IG_TOKEN", ""); igu = os.environ.get("IG_USER_ID") or "me"
    summary, failed = [], False
    if not have_x:
        summary.append("X: skipped (secrets not set)")
    if not ig:
        summary.append("Instagram: skipped (IG_TOKEN not set)")
    for p in paths:
        post = json.load(open(p, encoding="utf-8"))
        img = open(post["image"], "rb").read()
        if have_x:
            try:
                summary.append(f"X {post['id']}: {post_x(post, img, xk)}")
            except Exception as e:
                failed = True; summary.append(f"X {post['id']}: FAILED {e}")
        if ig:
            try:
                if not wait_live(post["image_url"]):
                    raise RuntimeError(f"{post['image_url']} not live after 15 min")
                summary.append(f"Instagram {post['id']}: {post_ig(post, ig, igu)}")
            except Exception as e:
                failed = True; summary.append(f"Instagram {post['id']}: FAILED {e}")
    print("\n".join(summary))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write("\n".join(f"- {s}" for s in summary) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if a.endswith(".json")])
