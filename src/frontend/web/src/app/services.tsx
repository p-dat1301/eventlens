import { createContext, type ReactNode, useContext } from "react"

import { type CatalogClient, createCatalogClient } from "../api/catalog"
import {
  type ChatClient,
  createChatClient,
  createSessionManager,
  type SessionManager,
} from "../api/chat"
import { createHttpClient, type FetchLike } from "../api/http"

export type Services = {
  readonly catalog: CatalogClient
  readonly chat: ChatClient
  readonly session: SessionManager
}

export const createServices = (options: { baseUrl?: string; fetch?: FetchLike } = {}): Services => {
  const http = createHttpClient(options)
  const session = createSessionManager(http)
  return { catalog: createCatalogClient(http), chat: createChatClient(http, session), session }
}

const ServicesContext = createContext<Services | null>(null)

export const ServicesProvider = ({
  services,
  children,
}: {
  readonly services: Services
  readonly children: ReactNode
}) => <ServicesContext.Provider value={services}>{children}</ServicesContext.Provider>

export const useServices = (): Services => {
  const services = useContext(ServicesContext)
  if (services === null) throw new Error("ServicesProvider missing")
  return services
}
