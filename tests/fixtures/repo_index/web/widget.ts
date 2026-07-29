import { format } from "./format";

export class WebWidget {
  render(): string {
    return format("ready");
  }
}

export function makeWidget(): WebWidget {
  return new WebWidget();
}
