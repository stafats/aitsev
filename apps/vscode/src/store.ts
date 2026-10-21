import type { TrackedTask } from "./model";

/** The subset of vscode.Memento the store needs (keeps the store testable). */
export interface MementoLike {
  get<T>(key: string, defaultValue: T): T;
  update(key: string, value: unknown): Thenable<void>;
}

const KEY = "aitsev.trackedTasks.v1";
const MAX_TRACKED = 200;

/** Tasks submitted from this workspace, with the snapshot needed for a safe apply. */
export class TaskStore {
  constructor(private readonly memento: MementoLike) {}

  all(): TrackedTask[] {
    return this.memento.get<TrackedTask[]>(KEY, []);
  }

  get(id: string): TrackedTask | undefined {
    return this.all().find((t) => t.id === id);
  }

  async add(task: TrackedTask): Promise<void> {
    const rest = this.all().filter((t) => t.id !== task.id);
    await this.memento.update(KEY, [task, ...rest].slice(0, MAX_TRACKED));
  }

  async update(id: string, patch: Partial<TrackedTask>): Promise<TrackedTask | undefined> {
    let updated: TrackedTask | undefined;
    const next = this.all().map((t) => {
      if (t.id !== id) return t;
      updated = { ...t, ...patch, id: t.id };
      return updated;
    });
    await this.memento.update(KEY, next);
    return updated;
  }
}
