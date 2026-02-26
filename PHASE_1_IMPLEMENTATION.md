"""
Angular Auth Service Example

This service can be used in the Angular frontend to authenticate with the Flask backend.
Place this file in: src/services/auth.service.ts
"""

import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, BehaviorSubject } from 'rxjs';

export interface UserInfo {
  user_id: string;
  email: string;
  name: string;
  picture?: string;
  authenticated: boolean;
}

@Injectable({
  providedIn: 'root'
})
export class AuthService {
  private readonly FLASK_API = 'http://localhost:5000';
  
  private userSubject = new BehaviorSubject<UserInfo | null>(null);
  public user$ = this.userSubject.asObservable();
  
  private isAuthenticatedSubject = new BehaviorSubject<boolean>(false);
  public isAuthenticated$ = this.isAuthenticatedSubject.asObservable();

  constructor(private http: HttpClient) {
    // Check if user is already logged in
    this.checkAuth();
  }

  /**
   * Check current authentication status
   */
  checkAuth(): void {
    this.http.get<UserInfo>(`${this.FLASK_API}/api/auth/check`, {
      withCredentials: true
    }).subscribe(
      (response: UserInfo) => {
        if (response.authenticated) {
          this.userSubject.next(response);
          this.isAuthenticatedSubject.next(true);
        }
      },
      (error) => {
        console.log('User not authenticated');
        this.userSubject.next(null);
        this.isAuthenticatedSubject.next(false);
      }
    );
  }

  /**
   * Initiate login flow by getting authorization URL
   */
  initiateLogin(): Observable<any> {
    return this.http.get(`${this.FLASK_API}/api/auth/login`, {
      withCredentials: true
    });
  }

  /**
   * Redirect to Google OAuth (call after initiateLogin)
   */
  redirectToLogin(): void {
    this.initiateLogin().subscribe(
      (response: any) => {
        window.location.href = response.authorization_url;
      },
      (error) => {
        console.error('Login initiation failed:', error);
      }
    );
  }

  /**
   * Get current user info (requires authentication)
   */
  getCurrentUser(): Observable<UserInfo> {
    return this.http.get<UserInfo>(`${this.FLASK_API}/api/auth/me`, {
      withCredentials: true
    });
  }

  /**
   * Logout user
   */
  logout(): Observable<any> {
    return this.http.post(`${this.FLASK_API}/api/auth/logout`, {}, {
      withCredentials: true
    });
  }

  /**
   * Logout and redirect
   */
  logoutAndRedirect(): void {
    this.logout().subscribe(
      () => {
        this.userSubject.next(null);
        this.isAuthenticatedSubject.next(false);
        window.location.href = 'http://localhost:4200';
      },
      (error) => {
        console.error('Logout failed:', error);
      }
    );
  }

  /**
   * Get user as synchronous value (if available)
   */
  getUserValue(): UserInfo | null {
    return this.userSubject.value;
  }

  /**
   * Check if user is authenticated
   */
  isAuthenticated(): boolean {
    return this.isAuthenticatedSubject.value;
  }
}
