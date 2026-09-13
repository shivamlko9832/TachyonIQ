/**
 * Server-side proxy to the UADA FastAPI backend.
 *
 * The browser only ever talks to this Next.js route (same origin, no CORS
 * needed). It forwards the request to BACKEND_URL and attaches
 * BACKEND_API_KEY (if set) as a Bearer token — the key never reaches the
 * client bundle.
 */
import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const BACKEND_API_KEY = process.env.BACKEND_API_KEY;

async function proxy(req: NextRequest, path: string[]): Promise<NextResponse> {
  const target = new URL(`${BACKEND_URL}/${path.join("/")}`);
  target.search = req.nextUrl.search;

  const headers = new Headers();
  const contentType = req.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  if (BACKEND_API_KEY) headers.set("authorization", `Bearer ${BACKEND_API_KEY}`);

  const hasBody = !["GET", "HEAD"].includes(req.method);

  let backendRes: Response;
  try {
    backendRes = await fetch(target.toString(), {
      method: req.method,
      headers,
      body: hasBody ? await req.text() : undefined,
      // @ts-expect-error -- Node fetch requires this for streaming request bodies with duplex
      duplex: hasBody ? "half" : undefined,
      cache: "no-store",
    });
  } catch (err) {
    return NextResponse.json(
      { detail: `Could not reach backend at ${BACKEND_URL}: ${(err as Error).message}` },
      { status: 502 },
    );
  }

  const resHeaders = new Headers();
  const respContentType = backendRes.headers.get("content-type");
  if (respContentType) resHeaders.set("content-type", respContentType);

  return new NextResponse(backendRes.body, {
    status: backendRes.status,
    headers: resHeaders,
  });
}

export async function GET(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}
export async function POST(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}
export async function DELETE(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}
export async function PATCH(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}
