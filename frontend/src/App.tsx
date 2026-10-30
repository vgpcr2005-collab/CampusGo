import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownLeft,
  ArrowRight,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Eye,
  EyeOff,
  FileText,
  GraduationCap,
  MapPin,
  Package,
  Plus,
  Search,
  ShieldCheck,
  ShoppingBag,
  Sparkles,
  Upload,
  Wallet,
  X,
  Zap,
} from "lucide-react";

type User = { id: number; name: string; email: string; available: boolean };
type Product = {
  id: number;
  name: string;
  category: string;
  description: string;
  price_paise: number;
  pricing_type: "bluebook" | "page" | "stationery";
  delivery_fee_paise: number;
  platform_fee_paise: number;
  platform_fee_per_page_paise: number;
};
type OrderItem = {
  product_name: string;
  quantity: number;
  unit_price_paise: number;
  page_count: number;
  copies: number;
  billable_pages: number;
  page_range: string;
  duplex: number;
};
type OrderDocument = { original_filename: string; byte_size: number; page_count: number };
type Order = {
  id: number;
  customer_id: number;
  delivery_user_id: number | null;
  destination: string;
  instructions: string;
  item_total_paise: number;
  delivery_fee_paise: number;
  platform_fee_paise: number;
  total_paise: number;
  status: string;
  payment_method: "online";
  payment_status: string;
  qr_payment?: { qr_code_id: string; image_url: string; close_by: number } | null;
  payment: {
    status: string;
    verification_status: string;
    gateway_fee_paise: number;
    gateway_tax_paise: number;
  } | null;
  allocations: { recipient_type: string; amount_paise: number; settlement_status: string }[];
  created_at: string;
  runner_name?: string;
  document?: OrderDocument | null;
  items: OrderItem[];
  history?: { status: string; created_at: string }[];
};
type Catalog = { products: Product[]; locations: string[] };
type PaymentConfig = {
  payment_configured: boolean;
  payment_mode: "test" | "live" | null;
  configuration_error: string | null;
  qr_payment_configured: boolean;
  qr_payment_configuration_error: string | null;
  route_enabled: boolean;
  route_shop_account_configured: boolean;
};
type OrdersPayload = { customer_orders: Order[]; active_deliveries: Order[]; available_orders: Order[] };
type AuthResponse = { user: User | null; csrf_token: string | null };
type CartLine = { quantity: number; copies: number };
type Cart = Record<number, CartLine>;
type CartTotals = {
  itemSubtotal: number;
  deliveryFee: number;
  platformFee: number;
  total: number;
  itemCount: number;
};
type UploadedDocument = {
  id: number;
  filename: string;
  size_bytes: number;
  page_count: number;
};
type View = "home" | "print" | "supplies" | "cart" | "orders" | "deliver";

const productImages: Record<string, { src: string; alt: string }> = {
  "Xerox copies (B&W)": {
    src: "/images/products/xerox-copies.svg",
    alt: "A neat stack of black-and-white copies beside a photocopier",
  },
  "Printout (B&W)": {
    src: "/images/products/printout-bw.svg",
    alt: "Black-and-white A4 documents coming out of a desktop printer",
  },
  "Printout (Colour)": {
    src: "/images/products/printout-colour.svg",
    alt: "Colour-printed A4 sheets with charts beside a printer",
  },
  "Bluebook — Basic": {
    src: "/images/products/bluebook-basic.svg",
    alt: "Blue-covered college answer book with visible ruled pages",
  },
  "Bluebook — Premium": {
    src: "/images/products/bluebook-premium.svg",
    alt: "Premium blue college answer book open to ruled pages",
  },
  "Record sheets": {
    src: "/images/products/record-sheets.svg",
    alt: "Open practical record book with ruled pages and a green cover",
  },
  Pen: {
    src: "/images/products/pen.svg",
    alt: "Blue ballpoint pen shown clearly from tip to clip",
  },
  "A4 sheets": {
    src: "/images/products/a4-sheets.svg",
    alt: "Neatly stacked white A4 paper sheets",
  },
  File: {
    src: "/images/products/file-folder.svg",
    alt: "Green document folder with papers visible inside",
  },
};

const viewFromHash = (hash: string): View => {
  const route = hash.replace(/^#/, "");
  return ["home", "print", "supplies", "cart", "orders", "deliver"].includes(route)
    ? (route as View)
    : "home";
};

const MAX_PDF_SIZE = 20 * 1024 * 1024;
const money = (paise: number) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: paise % 100 === 0 ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(paise / 100);
function selectedPages(pageRange: string, totalPages: number): number | null {
  if (pageRange === "all") return totalPages;
  if (!pageRange.trim() || !totalPages) return null;
  const pages = new Set<number>();
  for (const rawSegment of pageRange.split(",")) {
  const match = rawSegment.trim().match(/^(\d+)(?:-(\d+))?$/);
  if (!match) return null;
  const start = Number(match[1]);
  const end = Number(match[2] || match[1]);
  if (start < 1 || end < start || end > totalPages) return null;
  for (let page = start; page <= end; page += 1) {
    if (pages.has(page)) return null;
    pages.add(page);
  }
  }
  return pages.size ? pages.size : null;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

const prettyStatus = (status: string) => status.replace(/_/g, " ").toLowerCase();
const firstName = (name: string) => name.trim().split(/\s+/)[0] || name;

async function api<T>(
  url: string,
  method = "GET",
  body?: unknown,
  csrf?: string | null,
  extraHeaders: Record<string, string> = {},
): Promise<T> {
  const response = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      ...extraHeaders,
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = (await response.json().catch(() => ({}))) as T & { error?: string };
  if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

function calculateCart(products: Product[], cart: Cart, pagesToPrint: number | null): CartTotals {
  let itemSubtotal = 0;
  let deliveryFee = 0;
  let platformFee = 0;
  let stationeryFee = 0;
  let itemCount = 0;

  for (const product of products) {
    const line = cart[product.id];
    if (!line || (product.pricing_type === "page" ? line.copies <= 0 : line.quantity <= 0)) continue;
    const pageCount = pagesToPrint ?? 0;
    if (product.pricing_type === "page" && pageCount <= 0) continue;
    deliveryFee = Math.max(deliveryFee, product.delivery_fee_paise);
    if (product.pricing_type === "page") {
      const billablePages = pageCount * line.copies;
      itemSubtotal += product.price_paise * billablePages;
      platformFee += product.platform_fee_per_page_paise * billablePages;
      itemCount += line.copies;
    } else {
      itemSubtotal += product.price_paise * line.quantity;
      itemCount += line.quantity;
      if (product.pricing_type === "bluebook") {
        platformFee += product.platform_fee_paise * line.quantity;
      } else {
        stationeryFee = Math.max(stationeryFee, product.platform_fee_paise);
      }
    }
  }

  platformFee += stationeryFee;
  return {
    itemSubtotal,
    deliveryFee,
    platformFee,
    total: itemSubtotal + deliveryFee + platformFee,
    itemCount,
  };
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [csrf, setCsrf] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<Catalog>({ products: [], locations: [] });
  const [paymentConfig, setPaymentConfig] = useState<PaymentConfig>({
    payment_configured: false,
    payment_mode: null,
    configuration_error: "Checking Razorpay configuration…",
    qr_payment_configured: false,
    qr_payment_configuration_error: "Checking Razorpay QR configuration…",
    route_enabled: false,
    route_shop_account_configured: false,
  });
  const [orders, setOrders] = useState<OrdersPayload>({
    customer_orders: [],
    active_deliveries: [],
    available_orders: [],
  });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [view, setView] = useState<View>(() => viewFromHash(window.location.hash));
  const navigateToView = (nextView: View) => {
    setView(nextView);
    const hash = `#${nextView}`;
    if (window.location.hash !== hash) window.location.hash = hash;
    window.scrollTo({
      top: 0,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
  };
  const [cart, setCart] = useState<Cart>({});
  const [uploadedDocument, setUploadedDocument] = useState<UploadedDocument | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState("");
  const [pageRangeMode, setPageRangeMode] = useState<"all" | "selected">("all");
  const [customPageRange, setCustomPageRange] = useState("1");
  const [doubleSided, setDoubleSided] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("All items");
  const [destination, setDestination] = useState("");
  const [instructions, setInstructions] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [showAuth, setShowAuth] = useState(false);
  const [authName, setAuthName] = useState("");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const idempotencyKey = useRef<string | null>(null);
  const authDialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const syncViewWithHash = () => {
      setView(viewFromHash(window.location.hash));
    };
    window.addEventListener("hashchange", syncViewWithHash);
    return () => window.removeEventListener("hashchange", syncViewWithHash);
  }, []);

  useEffect(() => {
    if (loading) return;
    const revealElements = Array.from(
      document.querySelectorAll<HTMLElement>("[data-reveal]"),
    );
    if (!("IntersectionObserver" in window)) {
      revealElements.forEach((element) => element.classList.add("is-visible"));
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -24px 0px" },
    );
    revealElements.forEach((element) => {
      element.classList.remove("is-visible");
      observer.observe(element);
    });
    return () => observer.disconnect();
  }, [loading, view]);

  useEffect(() => {
    setCategory(view === "print" ? "All print" : view === "supplies" ? "All supplies" : "All items");
  }, [view]);

  const refreshOrders = useCallback(async () => {
    if (!user) return;
    const data = await api<OrdersPayload>("/api/orders");
    setOrders(data);
  }, [user]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setLoadError("");
    Promise.all([
      api<AuthResponse>("/api/me"),
      api<Catalog>("/api/catalog"),
      api<PaymentConfig>("/api/pricing"),
    ])
      .then(([auth, items, payment]) => {
        if (!active) return;
        setUser(auth.user);
        setCsrf(auth.csrf_token);
        setCatalog(items);
        setPaymentConfig(payment);
        setDestination(items.locations[0] || "");
        setLoading(false);
      })
      .catch((cause: Error) => {
        if (active) {
          setLoadError(cause.message);
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [loadAttempt]);

  useEffect(() => {
    if (!user) return;
    void refreshOrders().catch((cause: Error) => setError(cause.message));
    const timer = window.setInterval(() => {
      void refreshOrders().catch((cause: Error) => setError(cause.message));
    }, 8000);
    return () => window.clearInterval(timer);
  }, [user, refreshOrders]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 3800);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!showAuth) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const dialog = authDialogRef.current;
    const focusable = () =>
      Array.from(dialog?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      ) || []);
    focusable()[0]?.focus();

    const handleDialogKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowAuth(false);
        return;
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (!first || !last) {
        event.preventDefault();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleDialogKeyDown);
    return () => {
      document.removeEventListener("keydown", handleDialogKeyDown);
      previouslyFocused?.focus();
    };
  }, [showAuth]);

  const pageProducts = useMemo(() => {
    if (view === "print") return catalog.products.filter((item) => item.pricing_type === "page");
    if (view === "supplies") return catalog.products.filter((item) => item.pricing_type !== "page");
    return catalog.products;
  }, [catalog.products, view]);
  const categories = useMemo(
    () => [
      view === "print" ? "All print" : view === "supplies" ? "All supplies" : "All items",
      ...Array.from(new Set(pageProducts.map((item) => item.category))),
    ],
    [pageProducts, view],
  );
  const visibleProducts = useMemo(
    () =>
      pageProducts.filter(
        (item) =>
          (category.startsWith("All ") || item.category === category) &&
          `${item.name} ${item.description}`.toLowerCase().includes(search.toLowerCase()),
      ),
    [pageProducts, category, search],
  );
  const hasPrintingOrder = catalog.products.some(
    (product) => product.pricing_type === "page" && (cart[product.id]?.copies || 0) > 0,
  );
  const activePageRange = pageRangeMode === "all" ? "all" : customPageRange.trim();
  const pagesToPrint = uploadedDocument
    ? selectedPages(activePageRange, uploadedDocument.page_count)
    : null;
  const pageRangeError = pageRangeMode === "selected" && uploadedDocument && pagesToPrint === null
    ? `Enter valid, non-repeating pages between 1 and ${uploadedDocument.page_count}, e.g. 1-3,5.`
    : "";
  const cartTotals = useMemo(
    () => calculateCart(catalog.products, cart, pagesToPrint),
    [catalog.products, cart, pagesToPrint],
  );
  const cartCount = cartTotals.itemCount;

  const handleAuth = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = await api<AuthResponse>(
        `/api/auth/${authMode}`,
        "POST",
        authMode === "register"
          ? { name: authName, email: authEmail, password: authPassword }
          : { email: authEmail, password: authPassword },
      );
      setUser(data.user);
      setCsrf(data.csrf_token);
      setShowAuth(false);
      setAuthPassword("");
      setShowPassword(false);
      setNotice(authMode === "register" ? "Your student account is ready." : "Welcome back.");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    setBusy(true);
    try {
      await api("/api/auth/logout", "POST", {}, csrf);
      setUser(null);
      setCsrf(null);
      setOrders({ customer_orders: [], active_deliveries: [], available_orders: [] });
      navigateToView("home");
      setNotice("You have been signed out.");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const uploadDocument = async (file: File) => {
    if (
      !file.name.toLowerCase().endsWith(".pdf") ||
      (file.type && !["application/pdf", "application/x-pdf", "application/octet-stream"].includes(file.type))
    ) {
      setUploadError("Please choose a PDF file.");
      return;
    }
    if (file.size <= 0 || file.size > MAX_PDF_SIZE) {
      setUploadError(file.size > MAX_PDF_SIZE ? "PDF files must be 20 MB or smaller." : "The selected PDF is empty.");
      return;
    }

    setUploadError("");
    setUploadProgress(0);
    try {
      const nextDocument = await new Promise<UploadedDocument>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/documents");
        xhr.withCredentials = true;
        if (csrf) xhr.setRequestHeader("X-CSRF-Token", csrf);
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) setUploadProgress(Math.round((event.loaded / event.total) * 100));
        };
        xhr.onerror = () => reject(new Error("Upload failed. Check your connection and retry."));
        xhr.onload = () => {
          let response: { document?: UploadedDocument; error?: string };
          try {
            response = JSON.parse(xhr.responseText || "{}") as {
              document?: UploadedDocument;
              error?: string;
            };
          } catch {
            reject(new Error("The server returned an invalid upload response. Please retry."));
            return;
          }
          if (xhr.status < 200 || xhr.status >= 300 || !response.document) {
            reject(new Error(response.error || "The PDF could not be uploaded."));
            return;
          }
          resolve(response.document);
        };
        const formData = new FormData();
        formData.append("document", file);
        xhr.send(formData);
      });

      const previousDocument = uploadedDocument;
      setUploadedDocument(nextDocument);
      setCustomPageRange("1");
      setUploadProgress(100);
      if (previousDocument) {
        try {
          await api(`/api/documents/${previousDocument.id}`, "DELETE", undefined, csrf);
        } catch {
          setUploadError("New PDF uploaded. The previous unused upload could not be removed.");
        }
      }
    } catch (cause) {
      setUploadError((cause as Error).message);
    } finally {
      window.setTimeout(() => setUploadProgress(null), 800);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const removeUploadedDocument = async () => {
    if (!uploadedDocument) return;
    setUploadError("");
    try {
      await api(`/api/documents/${uploadedDocument.id}`, "DELETE", undefined, csrf);
      setUploadedDocument(null);
      setUploadProgress(null);
      setNotice("Uploaded document removed.");
    } catch (cause) {
      setUploadError((cause as Error).message);
    }
  };

  const retryPayment = async (orderId: number) => {
    setBusy(true);
    setError("");
    try {
      await api<{ payment_qr: Order["qr_payment"] }>(
        `/api/orders/${orderId}/payment-qr`,
        "POST",
        {},
        csrf,
      );
      await refreshOrders();
      setNotice("Order QR is ready. Scan it with a UPI app to pay.");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const verifyQrPayment = async (orderId: number) => {
    setBusy(true);
    setError("");
    try {
      const result = await api<{ payment_status: string; message?: string }>(
        `/api/orders/${orderId}/verify-qr-payment`,
        "POST",
        {},
        csrf,
      );
      setNotice(
        result.payment_status === "captured"
          ? "Payment verified by Razorpay. Your order is available for delivery."
          : result.message || "Payment is not confirmed yet. We will keep checking for updates.",
      );
      await refreshOrders();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submitOrder = async () => {
    if (!user) {
      setShowAuth(true);
      return;
    }
    if (cartCount === 0 || !destination) {
      setError("Choose at least one item and a delivery location.");
      return;
    }
    if (hasPrintingOrder && !uploadedDocument) {
      setUploadError("Upload the PDF you want to print or photocopy.");
      return;
    }
    if (hasPrintingOrder && (pagesToPrint === null || pageRangeError)) {
      setUploadError(pageRangeError || "Choose at least one valid page to print.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const currentCatalog = await api<Catalog>("/api/catalog");
      const currentTotals = calculateCart(currentCatalog.products, cart, pagesToPrint);
      if (currentTotals.total !== cartTotals.total) {
        setCatalog(currentCatalog);
        setError("The Xerox price changed. Review the updated cart total before placing your order.");
        return;
      }
      setCatalog(currentCatalog);
      idempotencyKey.current ||= window.crypto.randomUUID();
      const result = await api<{
        order: Order;
        payment_qr: Order["qr_payment"];
        payment_error: string | null;
      }>("/api/orders", "POST", {
        items: Object.entries(cart)
          .filter(([productId, line]) => {
            const product = catalog.products.find((item) => item.id === Number(productId));
            return product?.pricing_type === "page" ? line.copies > 0 : line.quantity > 0;
          })
          .map(([product_id, line]) => ({
            product_id,
            quantity: line.quantity,
            ...(catalog.products.find((product) => product.id === Number(product_id))?.pricing_type === "page"
              ? { copies: line.copies, page_range: activePageRange, duplex: doubleSided }
              : {}),
          })),
        document_id: hasPrintingOrder ? uploadedDocument?.id : undefined,
        destination,
        instructions,
        payment_flow: "qr",
        expected_total_paise: currentTotals.total,
      }, csrf, { "X-Idempotency-Key": idempotencyKey.current });
      idempotencyKey.current = null;
      setCart({});
      setInstructions("");
      setUploadedDocument(null);
      setPageRangeMode("all");
      setCustomPageRange("1");
      setDoubleSided(false);
      await refreshOrders();
      navigateToView("orders");
      if (!result.payment_qr && result.order.payment_status !== "captured") {
        setError(result.payment_error || "Payment setup is still pending. Retry from Order activity.");
        return;
      }
      setNotice("Order QR is ready. Scan it with a UPI app to pay.");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const action = async (path: string, method = "POST", body: unknown = {}) => {
    setBusy(true);
    setError("");
    try {
      await api(path, method, body, csrf);
      await refreshOrders();
      const meData = await api<AuthResponse>("/api/me");
      setUser(meData.user);
      setCsrf(meData.csrf_token);
      setNotice("Your update has been saved.");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return <div className="loading-screen" role="status" aria-live="polite" aria-busy="true"><span className="brand-mark"><Zap size={19} fill="currentColor" /></span><span className="loading-indicator" aria-hidden="true" /><span>Getting your campus ready…</span></div>;
  }

  if (loadError) {
    return (
      <main className="load-error-screen">
        <section className="load-error-card" role="alert">
          <span className="load-error-icon"><Package size={23} /></span>
          <div className="eyebrow">CAMPUSGO IS HAVING A MOMENT</div>
          <h1>We couldn't load your campus.</h1>
          <p>{loadError}</p>
          <button className="button button-dark" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>
            Try again <ArrowRight size={16} />
          </button>
        </section>
      </main>
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#home" onClick={(event) => { event.preventDefault(); navigateToView("home"); }}>
          <span className="brand-mark"><Zap size={19} fill="currentColor" /></span>
          <span>campus<span className="brand-accent">go</span></span>
        </a>
        <nav className="main-nav" aria-label="Main navigation">
          {([
            ["home", "Home"],
            ["print", "Print & Xerox"],
            ["supplies", "Stationery"],
            ["orders", "My orders"],
            ["deliver", "Deliver & earn"],
          ] as [View, string][]).map(([route, label]) => (
            <button key={route} className={view === route ? "nav-link active" : "nav-link"} aria-current={view === route ? "page" : undefined} onClick={() => navigateToView(route)}>{label}</button>
          ))}
        </nav>
        <div className="topbar-actions">
          <button className={view === "cart" ? "cart-nav active" : "cart-nav"} aria-label={`Cart, ${cartCount} items`} aria-current={view === "cart" ? "page" : undefined} onClick={() => navigateToView("cart")}><ShoppingBag size={18} /><span>Cart</span><b key={cartCount}>{cartCount}</b></button>
          {user ? (
            <>
              <span className="avatar">{user.name.slice(0, 1).toUpperCase()}</span>
              <span className="user-name">{firstName(user.name)}</span>
              <button className="icon-button" aria-label="Sign out" onClick={logout} disabled={busy}><ArrowDownLeft size={17} /></button>
            </>
          ) : (
            <button className="button button-outline button-small" onClick={() => setShowAuth(true)}>Log in <ArrowRight size={15} /></button>
          )}
        </div>
      </header>
      <nav className="mobile-nav" aria-label="Mobile navigation">
        {([
          ["home", "Home", <Zap size={18} key="home-icon" />],
          ["print", "Print", <FileText size={18} key="print-icon" />],
          ["supplies", "Supplies", <BookOpen size={18} key="supplies-icon" />],
          ["cart", "Cart", <ShoppingBag size={18} key="cart-icon" />],
          ["orders", "Orders", <Package size={18} key="orders-icon" />],
          ["deliver", "Earn", <Wallet size={18} key="earn-icon" />],
        ] as [View, string, React.ReactNode][]).map(([route, label, icon]) => (
          <button key={route} className={view === route ? "mobile-nav-link active" : "mobile-nav-link"} aria-current={view === route ? "page" : undefined} onClick={() => navigateToView(route)}>
            <span className="mobile-nav-icon">{icon}{route === "cart" && cartCount > 0 && <i key={cartCount}>{cartCount}</i>}</span><span>{label}</span>
          </button>
        ))}
      </nav>

      {error && <div className="toast toast-error" role="alert"><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError("")}><X size={15} /></button></div>}
      {notice && <div className="toast toast-success" role="status"><CheckCircle2 size={16} /><span>{notice}</span><button aria-label="Dismiss notification" onClick={() => setNotice("")}><X size={15} /></button></div>}

      {view !== "deliver" ? (
        <main key={view} className={`page-wrap page-${view}`}>
          {view === "home" && <>
          <section className="hero" data-reveal>
            <div className="hero-copy">
              <div className="eyebrow"><span className="eyebrow-dot" /> MADE FOR CAMPUS LIFE</div>
              <h1>Your campus needs,<br /><span>on the way.</span></h1>
              <p>Forgot your bluebook? Need a quick Xerox? Order in seconds and a fellow student will bring it to you.</p>
              <div className="hero-actions">
                <button className="button button-dark" onClick={() => navigateToView("supplies")}>Order what you need <ArrowRight size={16} /></button>
                <button className="button button-quiet" onClick={() => navigateToView("deliver")}><span className="earn-icon"><Wallet size={17} /></span> Earn between classes</button>
              </div>
              <div className="hero-note"><ShieldCheck size={15} /> By students, for students <span className="note-divider">·</span> One campus at a time</div>
            </div>
            <div className="hero-art" aria-label="Campus delivery illustration">
              <div className="art-halo" />
              <div className="art-grid" />
              <div className="campus-card">
                <div className="campus-sky"><div className="sun" /><div className="cloud cloud-a" /><div className="cloud cloud-b" /><div className="building"><div className="building-roof" /><div className="building-body"><div className="building-columns" /><div className="building-door" /></div></div><div className="ground" /><div className="path" /></div>
                <div className="parcel"><Package size={31} strokeWidth={1.6} /><span>YOUR ORDER</span></div>
                <div className="runner-figure"><div className="runner-head" /><div className="runner-body" /><div className="runner-leg leg-one" /><div className="runner-leg leg-two" /><div className="runner-bag"><Zap size={11} fill="currentColor" /></div></div>
                <div className="delivery-chip"><span><Check size={12} /></span> Ready for class</div>
              </div>
              <div className="floating-note note-top"><span className="float-icon float-green"><Clock3 size={15} /></span><span><b>Quick campus runs</b><small>Without leaving class</small></span></div>
              <div className="floating-note note-bottom"><span className="float-icon float-yellow"><BookOpen size={15} /></span><span><b>Bluebook, please!</b><small>Sorted in a few taps</small></span></div>
            </div>
          </section>

          <section className="trust-strip" aria-label="How CampusGo works" data-reveal>
            <div><span className="trust-icon"><ShoppingBag size={18} /></span><span><b>Pick your essentials</b><small>From campus favourites</small></span></div>
            <div className="trust-arrow"><ArrowRight size={16} /></div>
            <div><span className="trust-icon green"><GraduationCap size={19} /></span><span><b>A student picks it up</b><small>When they're available</small></span></div>
            <div className="trust-arrow"><ArrowRight size={16} /></div>
            <div><span className="trust-icon yellow"><MapPin size={18} /></span><span><b>Meet at your spot</b><small>Across your campus</small></span></div>
            <div className="strip-tag"><Sparkles size={14} /> SIMPLE AS THAT</div>
          </section>
          <section className="home-shortcuts" aria-labelledby="browse-title" data-reveal>
            <div className="section-heading compact">
              <div><div className="eyebrow">YOUR DAY, A LITTLE EASIER</div><h2 id="browse-title">What are we picking up?</h2><p>Start with a campus essential. Your order and fees stay clear at checkout.</p></div>
              <button className="button button-outline" onClick={() => navigateToView("orders")}>Track an order <ArrowRight size={15} /></button>
            </div>
            <div className="shortcut-grid">
              <button className="shortcut-card shortcut-print" onClick={() => navigateToView("print")}><span className="shortcut-icon"><FileText size={21} /></span><span className="shortcut-copy"><b>Print & Xerox</b><small>Upload a PDF and choose pages and copies.</small></span><ArrowRight size={18} /></button>
              <button className="shortcut-card shortcut-stationery" onClick={() => navigateToView("supplies")}><span className="shortcut-icon"><BookOpen size={21} /></span><span className="shortcut-copy"><b>Stationery & bluebooks</b><small>Browse available study essentials.</small></span><ArrowRight size={18} /></button>
            </div>
          </section>
          </>}

          {(view === "print" || view === "supplies" || view === "cart") && <section className={`content-grid ${view === "cart" ? "checkout-only-grid" : ""}`} id="catalog" data-reveal>
            {view === "cart" ? (
              <div className="cart-page-intro">
                <div className="eyebrow">READY WHEN YOU ARE</div><h1>Your campus cart.</h1>
                <p>Review your items, print settings and delivery location before creating a payment QR.</p>
                <button className="button button-outline" onClick={() => navigateToView("supplies")}>Keep browsing <ArrowRight size={15} /></button>
              </div>
            ) : (
            <div className="catalog-column">
              <div className="section-heading">
                <div><div className="eyebrow">{view === "print" ? "PRINTING, MADE SIMPLE" : "THE CAMPUS PICKUP LIST"}</div><h2>{view === "print" ? "Print & Xerox" : "Stationery & bluebooks"}</h2><p>{view === "print" ? "Choose a print option, then upload your PDF in the cart." : "Academic essentials, without the detour."}</p></div>
                <div className="search-box"><Search size={17} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Find an essential…" aria-label="Search items" /></div>
              </div>
              <div className="category-row">
                {categories.map((item) => <button key={item} className={category === item ? "category-pill selected" : "category-pill"} onClick={() => setCategory(item)}>{item}</button>)}
              </div>
              <div className="product-grid">
                {visibleProducts.map((product, index) => {
                  const line = cart[product.id];
                  const quantity = line?.quantity || 0;
                  const selected = product.pricing_type === "page" ? Boolean(line?.copies) : quantity > 0;
                  return (
                    <article className="product-card" key={product.id}>
                      <div className={`product-visual visual-${index % 4}`}>
                        {productImages[product.name] ? (
                          <>
                            <span className="visual-fallback" aria-hidden="true">
                              {product.pricing_type === "page" ? <FileText size={32} strokeWidth={1.4} /> : product.name.toLowerCase().includes("bluebook") ? <BookOpen size={32} strokeWidth={1.4} /> : <Package size={32} strokeWidth={1.4} />}
                            </span>
                            <img
                              src={productImages[product.name].src}
                              alt={productImages[product.name].alt}
                              width="600"
                              height="600"
                              loading="lazy"
                              decoding="async"
                              onLoad={(event) => {
                                event.currentTarget.classList.add("is-loaded");
                              }}
                              onError={(event) => {
                                event.currentTarget.hidden = true;
                              }}
                            />
                          </>
                        ) : (
                          product.pricing_type === "page" ? <FileText size={32} strokeWidth={1.4} /> : product.name.toLowerCase().includes("bluebook") ? <BookOpen size={32} strokeWidth={1.4} /> : <Package size={32} strokeWidth={1.4} />
                        )}
                        <span className="visual-tag">
                          {product.pricing_type === "page" ? "PER PAGE" : "AVAILABLE"}
                        </span>
                      </div>
                      <div className="product-info">
                        <span className="product-category">{product.category}</span>
                        <h3>{product.name}</h3>
                        <p>{product.description}</p>
                        <div className="product-bottom">
                          <strong>{money(product.price_paise)}{product.pricing_type === "page" && <small> / page</small>}</strong>
                          {product.pricing_type === "page" ? (
                            <button
                              className={selected ? "add-button selected-product" : "add-button"}
                              aria-label={`${selected ? "Selected" : "Select"} ${product.name}`}
                              onClick={() => setCart((current) => ({
                                ...current,
                                [product.id]: current[product.id] || { quantity: 0, copies: 1 },
                              }))}
                            >{selected ? <Check size={16} /> : <Plus size={17} />}</button>
                          ) : selected ? (
                            <div className="quantity-control">
                              <button aria-label={`Remove one ${product.name}`} onClick={() => setCart((current) => ({ ...current, [product.id]: { quantity: Math.max(0, quantity - 1), copies: 0 } }))}>−</button>
                              <span>{quantity}</span>
                              <button aria-label={`Add one ${product.name}`} onClick={() => setCart((current) => ({ ...current, [product.id]: { quantity: Math.min(20, quantity + 1), copies: 0 } }))}>+</button>
                            </div>
                          ) : (
                            <button
                              className="add-button"
                              aria-label={`Add ${product.name} to cart`}
                              onClick={() => setCart((current) => ({ ...current, [product.id]: { quantity: 1, copies: 0 } }))}
                            ><Plus size={17} /></button>
                          )}
                        </div>
                      </div>
                    </article>
                  );
                })}
                {visibleProducts.length === 0 && <div className="empty-search"><Search size={21} /><b>No matches just yet</b><span>Try another search or category.</span><button className="button button-outline button-small" onClick={() => { setSearch(""); setCategory(view === "print" ? "All print" : "All supplies"); }}>Clear filters</button></div>}
              </div>
            </div>
            )}

            {view === "cart" ? <aside className="checkout-card">
              <div className="checkout-head"><div><span className="eyebrow">YOUR CAMPUS RUN</span><h3>Order summary</h3></div><span key={cartCount} className="cart-count">{cartCount}</span></div>
              {cartCount ? (
                <div className="cart-items">
                  {catalog.products.filter((item) => cart[item.id] && (item.pricing_type === "page" ? cart[item.id].copies > 0 : cart[item.id].quantity > 0)).map((item) => {
                    const line = cart[item.id];
                    const billablePages = item.pricing_type === "page" ? (pagesToPrint || 0) * line.copies : 0;
                    const lineTotal = item.price_paise * (item.pricing_type === "page" ? billablePages : line.quantity);
                    return (
                      <div className="cart-line cart-line-detail" key={item.id} data-cart-item>
                        <span>{item.name}
                          {item.pricing_type === "page" ? (
                            <>
                            <small className="print-price-note">{money(item.price_paise)} / page · {pagesToPrint || 0} pages × {line.copies} copies</small>
                            <span className="print-options">
                              <span>{pagesToPrint || 0} pages</span>
                              <span>×</span>
                              <label>Copies<input aria-label={`${item.name} copies`} type="number" min="1" max="20" value={line.copies} onChange={(event) => setCart((current) => ({ ...current, [item.id]: { ...line, copies: Math.max(1, Math.min(20, Number(event.target.value) || 1)) } }))} /></label>
                              <button className="remove-line" aria-label={`Remove ${item.name}`} onClick={() => setCart((current) => { const updated = { ...current }; delete updated[item.id]; return updated; })}><X size={13} /></button>
                            </span>
                            </>
                          ) : <small>Qty {line.quantity}</small>}
                        </span>
                        <b>{money(lineTotal)}</b>
                      </div>
                    );
                  })}
                </div>
              ) : <div className="cart-empty"><ShoppingBag size={22} /><span>Your next campus run<br />starts with an item.</span></div>}
              {hasPrintingOrder && (
                <section className="document-upload" aria-labelledby="document-upload-title">
                  <div className="upload-heading">
                    <span className="upload-icon"><FileText size={17} /></span>
                    <div><h4 id="document-upload-title">Upload Your Document</h4><p>Upload the PDF you want to print or photocopy.</p></div>
                  </div>
                  <input
                    ref={fileInput}
                    className="visually-hidden-file"
                    type="file"
                    accept="application/pdf,.pdf"
                    aria-label="Choose a PDF file"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void uploadDocument(file);
                      event.currentTarget.value = "";
                    }}
                  />
                  {!uploadedDocument ? (
                    <button className="button button-outline choose-pdf-button" onClick={() => fileInput.current?.click()} disabled={uploadProgress !== null}>
                      <Upload size={15} /> Choose PDF File
                    </button>
                  ) : (
                    <div className="uploaded-file-card">
                      <span className="uploaded-file-icon"><CheckCircle2 size={17} /></span>
                      <span className="uploaded-file-name"><b>{uploadedDocument.filename}</b><small>{formatBytes(uploadedDocument.size_bytes)} · {uploadedDocument.page_count} {uploadedDocument.page_count === 1 ? "page" : "pages"}</small></span>
                      <button className="file-action" onClick={() => fileInput.current?.click()} disabled={uploadProgress !== null}>Replace</button>
                      <button className="file-remove" onClick={() => void removeUploadedDocument()} disabled={uploadProgress !== null} aria-label="Remove uploaded PDF"><X size={15} /></button>
                    </div>
                  )}
                  {uploadProgress !== null && (
                    <div className="upload-progress" role="status" aria-live="polite">
                      <div><span>{uploadProgress === 100 ? "PDF uploaded" : "Uploading PDF…"}</span><b>{uploadProgress}%</b></div>
                      <progress max="100" value={uploadProgress} aria-label="PDF upload progress" />
                    </div>
                  )}
                  {uploadError && <p className="upload-error" role="alert">{uploadError}</p>}
                  {uploadedDocument && (
                    <div className="document-options">
                      <div className="document-option-label">PRINTING TYPE</div>
                      <div className="selected-print-types">{catalog.products.filter((item) => item.pricing_type === "page" && cart[item.id]?.copies > 0).map((item) => item.name).join(" · ")}</div>
                      <label className="document-option-label" htmlFor="page-range-mode">PAGES TO PRINT</label>
                      <select id="page-range-mode" className="text-input" value={pageRangeMode} onChange={(event) => setPageRangeMode(event.target.value as "all" | "selected")}>
                        <option value="all">All pages ({uploadedDocument.page_count})</option>
                        <option value="selected">Selected page range</option>
                      </select>
                      {pageRangeMode === "selected" && (
                        <label className="page-range-field">Page range
                          <input className="text-input" value={customPageRange} onChange={(event) => setCustomPageRange(event.target.value)} placeholder="e.g. 1-3,5" aria-label="Selected PDF page range" />
                        </label>
                      )}
                      {pageRangeError && <p className="upload-error" role="alert">{pageRangeError}</p>}
                      <label className="duplex-option"><input type="checkbox" checked={doubleSided} onChange={(event) => setDoubleSided(event.target.checked)} /> Print double-sided</label>
                      <div className="billable-pages">Billable pages <b>{(pagesToPrint || 0) * catalog.products.reduce((sum, item) => sum + (item.pricing_type === "page" ? cart[item.id]?.copies || 0 : 0), 0)}</b></div>
                    </div>
                  )}
                </section>
              )}
              <label className="form-label" htmlFor="drop-location">DROP-OFF LOCATION</label>
              <div className="select-wrap"><MapPin size={16} /><select id="drop-location" value={destination} onChange={(event) => setDestination(event.target.value)}>{catalog.locations.map((location) => <option key={location}>{location}</option>)}</select><ChevronDown size={15} /></div>
              <label className="form-label" htmlFor="order-notes">DELIVERY NOTE <span>OPTIONAL</span></label>
              <input id="order-notes" className="text-input" value={instructions} maxLength={300} onChange={(event) => setInstructions(event.target.value)} placeholder="e.g. Outside the CSE lab" />
              <div className="form-label payment-label">PAYMENT</div>
              <div className="payment-options">
                <div className="payment-option chosen"><span className="radio-dot" /><span><b>UPI QR payment</b><small>Scan a unique, fixed-amount QR code for this order.</small></span></div>
              </div>
              {!paymentConfig.qr_payment_configured && <div className="payment-config-error" role="alert">{paymentConfig.qr_payment_configuration_error || "Razorpay QR payments are not configured. Contact the CampusGo administrator."}</div>}
              {paymentConfig.payment_mode === "test" && <div className="payment-mode-note">Razorpay test mode · no real payment will be taken.</div>}
              {paymentConfig.payment_configured && (!paymentConfig.route_enabled || !paymentConfig.route_shop_account_configured) && <div className="payment-mode-note">Marketplace transfers are not configured. Payment can be collected, but recipient settlement remains pending.</div>}
              <div className="price-breakdown">
                <div><span>Item subtotal</span><b>{money(cartTotals.itemSubtotal)}</b></div>
                <div><span>Delivery <small>(one fee per order; runner earns)</small></span><b>{money(cartTotals.deliveryFee)}</b></div>
                <div><span>Platform fee</span><b>{money(cartTotals.platformFee)}</b></div>
                <div className="total-line"><span>Total</span><b key={`${cartCount}-${cartTotals.total}`}>{money(cartTotals.total)}</b></div>
              </div>
              <button className="button button-dark checkout-button" onClick={submitOrder} disabled={busy || !paymentConfig.qr_payment_configured || uploadProgress !== null || cartCount === 0 || (hasPrintingOrder && (!uploadedDocument || pagesToPrint === null || Boolean(pageRangeError)))}>{busy ? "One moment…" : "Generate payment QR"} <ArrowRight size={16} /></button>
              <div className="secure-note"><ShieldCheck size={14} /> Clear pricing. No surprise fees.</div>
              <div className="settlement-note"><b>How the total works</b><span>One delivery fee is charged per order. Printing is priced by page × copies. Your item subtotal, delivery fee, and CampusGo platform fee are shown separately above.</span></div>
            </aside> : <aside className="catalog-cart-preview">
              <span className="eyebrow">YOUR CAMPUS RUN</span><b>{cartCount ? `${cartCount} ${cartCount === 1 ? "item" : "items"} in your cart` : "Your cart is ready"}</b>
              <span>{money(cartTotals.total)} <small>including shown fees</small></span>
              <button className="button button-dark" onClick={() => navigateToView("cart")}>{cartCount ? "Review & checkout" : "View cart"} <ArrowRight size={15} /></button>
            </aside>}
          </section>}

          {view === "orders" && user && (
            <section className="orders-section" data-reveal>
              <div className="section-heading compact"><div><div className="eyebrow">YOUR CAMPUS RUNS</div><h2>Order activity</h2></div><span className="live-tag"><span /> Updates every few seconds</span></div>
              {orders.customer_orders.length ? <div className="orders-list">{orders.customer_orders.map((order) => <OrderCard key={order.id} order={order} mode="customer" onConfirm={() => action(`/api/orders/${order.id}/confirm`)} onPay={() => retryPayment(order.id)} onVerifyPayment={() => verifyQrPayment(order.id)} disabled={busy} />)}</div> : <div className="quiet-empty"><Package size={20} /><span>Your placed orders will show up here.</span></div>}
            </section>
          )}
          {view === "orders" && !user && <section className="orders-guest">
            <span className="load-error-icon"><Package size={23} /></span><div className="eyebrow">YOUR CAMPUS RUNS</div><h1>Order updates, all in one place.</h1><p>Log in to see order status, payment updates and delivery progress.</p><button className="button button-dark" onClick={() => setShowAuth(true)}>Log in to view orders <ArrowRight size={15} /></button>
          </section>}
          <Footer />
        </main>
      ) : (
        <main key={view} className="runner-page">
          <section className="runner-hero" data-reveal>
            <div>
              <div className="eyebrow"><span className="eyebrow-dot" /> YOUR FREE TIME HAS POTENTIAL</div>
              <h1>Between classes?<br /><span>Be the reason</span><br />someone makes it.</h1>
              <p>Pick up a campus order on your way. Earn a delivery fee on your schedule, one student helping another.</p>
              {!user && <button className="button button-dark" onClick={() => setShowAuth(true)}>Join as a student <ArrowRight size={16} /></button>}
              <div className="runner-points"><span><CheckCircle2 size={15} /> No fixed shifts</span><span><CheckCircle2 size={15} /> Your campus, your pace</span></div>
            </div>
            <div className="earn-card">
              <div className="earn-card-icon"><Wallet size={19} /></div><span className="eyebrow">PER COMPLETED RUN</span>
              <div className="earn-amount">{catalog.products.length ? `${money(Math.min(...catalog.products.map((item) => item.delivery_fee_paise)))}–${money(Math.max(...catalog.products.map((item) => item.delivery_fee_paise)))}` : money(0)}</div>
              <div className="earn-description">Delivery fee depends on the selected items and goes to the student who completes the run.</div>
              <div className="earn-divider" />
              <div className="earn-example"><span><span className="earn-dot dot-green" /> Your delivery fee</span><b>Per order</b></div>
              <div className="earn-example"><span><span className="earn-dot dot-yellow" /> CampusGo service fee</span><b>Itemized at checkout</b></div>
              <div className="earn-footnote">The shop's item cost and CampusGo fee are accounted separately. Checkout shows every charge before the order is placed.</div>
            </div>
          </section>

          {user ? (
            <section className="runner-dashboard" data-reveal>
              <div className="availability-panel">
                <div><div className="eyebrow">YOUR AVAILABILITY</div><h2>Take a campus run?</h2><p>Go online when you're free. One active delivery at a time.</p></div>
                <button className={user.available ? "availability-toggle online" : "availability-toggle"} onClick={() => action("/api/availability", "POST", { available: !user.available })} disabled={busy}><span className="toggle-dot" />{user.available ? "Available now" : "Go available"}</button>
              </div>
              <div className="runner-columns">
                <div className="runner-list-column">
                  <div className="section-heading compact"><div><div className="eyebrow">OPEN CAMPUS ORDERS</div><h2>Pick a run</h2></div><span className="result-count">{orders.available_orders.length} open</span></div>
                  {!user.available ? <div className="runner-empty"><div className="empty-icon"><Zap size={20} /></div><b>Go available to see open orders</b><span>Only take a run when you're free to finish it.</span></div> : orders.available_orders.length ? <div className="orders-list">{orders.available_orders.map((order) => <OrderCard key={order.id} order={order} mode="available" onAccept={() => action(`/api/orders/${order.id}/accept`)} disabled={busy} />)}</div> : <div className="runner-empty"><div className="empty-icon"><Clock3 size={20} /></div><b>All quiet on campus</b><span>New requests will appear here automatically.</span></div>}
                </div>
                <div className="runner-list-column active-column">
                  <div className="section-heading compact"><div><div className="eyebrow">YOUR ACTIVE RUN</div><h2>In progress</h2></div></div>
                  {orders.active_deliveries.length ? <div className="orders-list">{orders.active_deliveries.map((order) => <OrderCard key={order.id} order={order} mode="runner" onNextStatus={() => action(`/api/orders/${order.id}/status`, "POST", { status: nextStatus(order.status) })} disabled={busy} />)}</div> : <div className="runner-empty"><div className="empty-icon"><Package size={20} /></div><b>No active deliveries</b><span>Accept an open campus order to get started.</span></div>}
                </div>
              </div>
            </section>
          ) : <section className="join-banner" data-reveal><div className="join-icon"><GraduationCap size={22} /></div><div><b>Your campus. Your schedule. Your call.</b><span>Create your student account to see nearby delivery requests.</span></div><button className="button button-dark" onClick={() => setShowAuth(true)}>Get started <ArrowRight size={16} /></button></section>}
          <Footer />
        </main>
      )}

      {showAuth && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowAuth(false); }}><div ref={authDialogRef} className="auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-title"><button className="modal-close" onClick={() => setShowAuth(false)} aria-label="Close"><X size={18} /></button><div className="auth-brand"><span className="brand-mark"><Zap size={18} fill="currentColor" /></span> campus<span className="brand-accent">go</span></div><div className="eyebrow">STUDENT ACCESS</div><h2 id="auth-title">{authMode === "login" ? "Good to see you." : "Campus starts here."}</h2><p>{authMode === "login" ? "Log in to place orders and pick up a campus run." : "Create one account to order or deliver on campus."}</p><form key={authMode} onSubmit={handleAuth}>{authMode === "register" && <label className="auth-field">Your name<input autoComplete="name" required maxLength={80} value={authName} onChange={(event) => setAuthName(event.target.value)} placeholder="e.g. Ananya Rao" /></label>}<label className="auth-field">Student email<input type="email" autoComplete="email" required value={authEmail} onChange={(event) => setAuthEmail(event.target.value)} placeholder="you@college.edu" /></label><div className="auth-field"><label htmlFor="auth-password">Password</label><div className="auth-password-input"><input id="auth-password" type={showPassword ? "text" : "password"} autoComplete={authMode === "login" ? "current-password" : "new-password"} required minLength={authMode === "register" ? 10 : undefined} value={authPassword} onChange={(event) => setAuthPassword(event.target.value)} placeholder={authMode === "register" ? "At least 10 characters" : "Your password"} /><button className="password-visibility" type="button" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword} onClick={() => setShowPassword((visible) => !visible)}>{showPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></div><button className="button button-dark auth-submit" disabled={busy}>{busy ? "One moment…" : authMode === "login" ? "Log in" : "Create student account"} <ArrowRight size={16} /></button></form><div className="auth-switch">{authMode === "login" ? "New to campusgo?" : "Already have an account?"}<button onClick={() => { setAuthMode(authMode === "login" ? "register" : "login"); setShowPassword(false); }}>{authMode === "login" ? "Create an account" : "Log in"}</button></div><div className="auth-privacy"><ShieldCheck size={14} /> Your password is stored securely as a hash.</div></div></div>}
    </div>
  );
}

function nextStatus(status: string) {
  const flow: Record<string, string> = {
    ACCEPTED: "PICKUP_IN_PROGRESS",
    PICKUP_IN_PROGRESS: "ITEMS_COLLECTED",
    ITEMS_COLLECTED: "OUT_FOR_DELIVERY",
    OUT_FOR_DELIVERY: "DELIVERED",
  };
  return flow[status] || "";
}

function OrderCard({
  order,
  mode,
  onAccept,
  onNextStatus,
  onConfirm,
  onPay,
  onVerifyPayment,
  disabled,
}: {
  order: Order;
  mode: "customer" | "available" | "runner";
  onAccept?: () => void;
  onNextStatus?: () => void;
  onConfirm?: () => void;
  onPay?: () => void;
  onVerifyPayment?: () => void;
  disabled: boolean;
}) {
  const isComplete = order.status === "COMPLETED";
  const step = ["PLACED", "ACCEPTED", "PICKUP_IN_PROGRESS", "ITEMS_COLLECTED", "OUT_FOR_DELIVERY", "DELIVERED", "COMPLETED"].indexOf(order.status);
  const nextLabel: Record<string, string> = {
    ACCEPTED: "Heading to shop",
    PICKUP_IN_PROGRESS: "Items collected",
    ITEMS_COLLECTED: "Start delivery",
    OUT_FOR_DELIVERY: "Mark delivered",
  };
  const paymentStatusLabels: Record<string, string> = {
    failed: "Payment failed",
    cancelled: "Payment cancelled",
    authorized: "Payment authorized",
    partially_refunded: "Partially refunded",
    refunded: "Refunded",
  };
  const statusLabel = paymentStatusLabels[order.payment_status]
    || (order.status === "PAYMENT_PENDING" ? "Payment pending" : isComplete ? "Completed" : prettyStatus(order.status));
  const currentHistoryStep = Math.min(step, (order.history?.length || 1) - 1);
  return (
    <article className="order-card">
      <div className="order-card-top"><div><span className="order-id">RUN #{String(order.id).padStart(4, "0")}</span><span className={`status-badge ${isComplete ? "status-done" : order.status === "PAYMENT_PENDING" ? "status-pending" : ""}`}><span />{statusLabel}</span></div><span className="order-total">{money(order.total_paise)}</span></div>
      <div className="order-route"><span><MapPin size={14} />Pickup</span><ArrowRight size={14} /><b>{order.destination}</b></div>
      <div className="order-item-summary">{order.items.map((item) => item.billable_pages > 0
        ? `${item.page_count} pages${item.page_range !== "all" ? ` (${item.page_range})` : ""} × ${item.copies} copies${item.duplex ? " · double-sided" : ""} ${item.product_name}`
        : `${item.quantity} × ${item.product_name}`).join(" · ")}</div>
      {order.instructions && <div className="order-instructions">“{order.instructions}”</div>}
      {mode === "customer" && order.document && <a className="order-document-link" href={`/api/orders/${order.id}/document`}><FileText size={13} />{order.document.original_filename} · {order.document.page_count} pages <span>Download PDF</span></a>}
      <div className="order-meta"><span><Clock3 size={13} />{new Date(order.created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span><span><Wallet size={13} />{money(order.delivery_fee_paise)} delivery fee</span>{order.runner_name && <span><GraduationCap size={14} />{order.runner_name}</span>}</div>
      {mode === "customer" && <div className="payment-status"><ShieldCheck size={14} />{order.payment_status === "captured" ? "Payment verified by Razorpay" : order.payment_status === "authorized" ? "Payment authorized; waiting for capture confirmation" : order.payment_status === "failed" ? "Payment failed. This order is not yet confirmed." : order.payment_status === "cancelled" ? "Payment cancelled. This order is not yet confirmed." : order.payment_status === "refunded" || order.payment_status === "partially_refunded" ? "Payment refund update received from Razorpay." : "Payment pending verification"}</div>}
      {mode === "customer" && order.payment_status === "captured" && order.allocations.some((allocation) => ["route_unavailable", "linked_account_unavailable", "amount_unavailable_after_gateway_fees", "pending_verification"].includes(allocation.settlement_status)) && <div className="payment-status">Marketplace settlement is pending or unavailable for this account.</div>}
      {mode === "customer" && order.payment_status !== "captured" && order.qr_payment && (
        <div className="order-qr">
          <img src={order.qr_payment.image_url} alt={`Payment QR for order ${order.id}`} referrerPolicy="no-referrer" />
          <div><b>Scan to pay {money(order.total_paise)}</b><span>Single-use UPI QR · valid until {new Date(order.qr_payment.close_by * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span></div>
          <button className="button button-green order-action" onClick={onVerifyPayment} disabled={disabled}>I've paid — verify <Check size={14} /></button>
        </div>
      )}
      {mode === "customer" && order.status === "PAYMENT_PENDING" && order.payment_status !== "authorized" && !["refunded", "partially_refunded"].includes(order.payment_status) && (!order.qr_payment || order.qr_payment.close_by <= Math.floor(Date.now() / 1000)) && <button className="button button-dark order-action" onClick={onPay} disabled={disabled}>{order.qr_payment ? "Generate a new QR" : "Generate payment QR"} <ArrowRight size={15} /></button>}
      {mode === "customer" && order.status === "DELIVERED" && <button className="button button-green order-action" onClick={onConfirm} disabled={disabled}>Confirm delivery <Check size={15} /></button>}
      {mode === "runner" && onNextStatus && nextLabel[order.status] && <button className="button button-dark order-action" onClick={onNextStatus} disabled={disabled}>{nextLabel[order.status]} <ArrowRight size={15} /></button>}
      {mode === "available" && <button className="button button-green order-action" onClick={onAccept} disabled={disabled}>Accept this run <ArrowRight size={15} /></button>}
      {mode !== "available" && order.history && <ol key={`${order.status}-${order.history.length}`} className="order-progress" aria-label={`Delivery progress for order ${order.id}`}>{order.history.map((entry, index) => <li key={`${entry.status}-${entry.created_at}`} className={index <= step ? "progress-step complete" : "progress-step"} aria-current={index === currentHistoryStep ? "step" : undefined}><i aria-hidden="true" /><span>{prettyStatus(entry.status)}</span></li>)}</ol>}
    </article>
  );
}

function Footer() {
  return <footer className="footer"><a className="brand footer-brand" href="#home"><span className="brand-mark"><Zap size={16} fill="currentColor" /></span><span>campus<span className="brand-accent">go</span></span></a><span>Small campus runs. A little more breathing room.</span><span className="footer-pilot"><span /> PILOT VERSION</span></footer>;
}

export default App;
