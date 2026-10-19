import { ApiError } from "../api/client";

export function LoadingState({ label }: { label: string }) {
  return (
    <p className="state" role="status">
      {label}
    </p>
  );
}

export function EmptyState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="state-card">
      <h2>{title}</h2>
      {detail ? <p>{detail}</p> : null}
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  const message = messageFor(error);
  return (
    <div className="state-card error" role="alert">
      <h2>Δεν ήταν δυνατή η φόρτωση</h2>
      <p>{message}</p>
    </div>
  );
}

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "network") {
      return "Το API δεν απάντησε. Τρέξε το uvicorn μόνο στο 127.0.0.1:8000 και άνοιξε το frontend από το Vite dev server.";
    }
    if (error.code === "no_snapshots") {
      return "Δεν υπάρχουν snapshots ακόμα. Τρέξε πρώτα ένα universe scan.";
    }
    if (error.code === "symbol_not_found") {
      return "Το σύμβολο δεν υπάρχει στο τελευταίο operational scan.";
    }
    if (error.code === "bad_response") {
      return "Η απόκριση του API δεν έχει το αναμενόμενο σχήμα.";
    }
  }
  return "Κάτι πήγε στραβά κατά την ανάγνωση του scan.";
}
