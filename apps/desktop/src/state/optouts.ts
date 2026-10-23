export interface StorageLike {
  getItem(k: string): string | null;
  setItem(k: string, value: string): void;
  removeItem(k: string): void;
}
export interface OptOut {
  id: string;
  phone: string;
  reason: string;
  created_at: string;
}
export class OptOutJournal {
  private key: string;
  constructor(
    private storage: StorageLike,
    tenant: string,
  ) {
    this.key = `opendialer.optouts.${tenant}`;
  }
  get pending(): OptOut[] {
    return JSON.parse(this.storage.getItem(this.key) ?? "[]") as OptOut[];
  }
  get blocksCalling(): boolean {
    return this.pending.length > 0;
  }
  add(phone: string, reason: string): OptOut {
    const event = {
      id: crypto.randomUUID(),
      phone,
      reason,
      created_at: new Date().toISOString(),
    };
    this.storage.setItem(this.key, JSON.stringify([...this.pending, event]));
    return event;
  }
  async synchronize(send: (event: OptOut) => Promise<unknown>): Promise<void> {
    for (const event of this.pending) {
      try {
        await send(event);
        this.storage.setItem(
          this.key,
          JSON.stringify(this.pending.filter((x) => x.id !== event.id)),
        );
      } catch {
        break;
      }
    }
  }
}
