import { useEffect, useRef } from "react";

import shell from "../legacy-body.html?raw";
import { mountLegacyController } from "./legacy-controller";

export function App() {
  const mounted = useRef(false);

  useEffect(() => {
    if (mounted.current) return;
    mounted.current = true;
    mountLegacyController();
  }, []);

  return <div dangerouslySetInnerHTML={{ __html: shell }} />;
}
